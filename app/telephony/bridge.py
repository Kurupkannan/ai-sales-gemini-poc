"""Bounded, multi-turn Gemini audio bridge with provider playback cancellation."""
import asyncio
import base64
import logging
import time

import numpy as np
from contextlib import suppress
from starlette.websockets import WebSocketDisconnect
from app.audio.converter import StreamingResampler
from app.telephony import runtime
from app.telephony.preroll import HANGUP_CHECKPOINT, begin, cancel_hangup, stop
from app.gemini.agent_prompt import SILENCE_CHECK, SILENCE_CLOSE, WRAP_UP, no_reply_nudge
from app.config import settings
from app.timing import call_mark
from app.plivo.websocket import parse_message, decode_media, validate_start
from app.telephony.service import terminate

logger = logging.getLogger(__name__)


async def watch_silence(audio, attempt, tick: float = 1):
    """Gemini only answers speech, so a silent line would otherwise stay open until the time limit.

    After CALL_SILENCE_SECONDS of quiet: ask the agent to check in; after another quiet spell, to
    close; after a third, return so the caller hangs up. Caller speech resets the count.
    """
    checks, turns, nudged = 0, audio.caller_turns, 0
    # The greeting is prepared while the phone rings; silence only counts from pickup.
    audio.last_heard = max(audio.last_heard, time.monotonic())
    while True:
        await asyncio.sleep(tick)
        if audio.hangup.is_set():
            continue  # A goodbye is playing; resume if the caller interrupts it.
        if audio.caller_turns != turns:
            checks, turns = 0, audio.caller_turns
        quiet_since = max(audio.caller_spoke_at, audio.caller_voice_at)
        if (audio.caller_turns != nudged and audio.caller_spoke_at > audio.agent_audio_at
                and not agent_speaking(audio)
                and time.monotonic() - quiet_since > settings.CALL_REPLY_NUDGE_SECONDS):
            # Only once the caller has stopped talking: never push the agent to talk over them.
            # They spoke and the agent stayed quiet (e.g. after a bare "hello"): make it answer.
            nudged = audio.caller_turns
            logger.info('Agent did not reply; nudging attempt=%s', attempt.id)
            await audio.manager.send_instruction(audio.session, no_reply_nudge(audio.caller_said))
            continue
        if not audio.queue.empty() or time.monotonic() - audio.last_heard < settings.CALL_SILENCE_SECONDS:
            continue
        checks += 1
        if checks > 2:
            logger.info('No response; hanging up attempt=%s', attempt.id)
            return
        audio.last_heard = time.monotonic()
        await audio.manager.send_instruction(audio.session, SILENCE_CHECK if checks == 1 else SILENCE_CLOSE)


def agent_speaking(audio) -> bool:
    return not audio.queue.empty() or time.monotonic() - audio.agent_played_at < 0.3


async def on_caller_audio(audio, raw: bytes, session):
    """Voice activity from the caller's own audio, ahead of Gemini's transcription.

    Gemini writes a reply faster than it is spoken, so it has usually finished by the time the
    caller talks over it and never reports an interruption. Stop our playback ourselves.
    """
    now = time.monotonic()
    loud = bool(raw) and float(np.sqrt(np.mean(np.frombuffer(raw, '<i2').astype(np.float32) ** 2))) > settings.CALLER_VOICE_RMS
    if not loud:
        audio.voice_started, audio.barged = None, False
        return
    audio.last_heard = audio.caller_voice_at = now
    if audio.voice_started is None:
        audio.voice_started = now
    if (not audio.barged and agent_speaking(audio)
            and now - audio.voice_started >= settings.CALL_BARGE_IN_SECONDS):
        audio.barged = True
        generation = await audio.interrupt()
        if generation is not None:
            await audio.push(generation, {'event': 'clearAudio', 'streamId': audio.stream_id})
        await cancel_hangup(audio, session, stop_playback=False)


async def wrap_up_after(audio, attempt, limit: float, grace: float):
    """Keep calls short whatever the caller says: ask the agent to close, then hang up ourselves."""
    await asyncio.sleep(limit)
    if not audio.hangup.is_set():
        logger.info('Call time limit reached; asking agent to close attempt=%s', attempt.id)
        await audio.manager.send_instruction(audio.session, WRAP_UP)
    while True:
        await asyncio.sleep(grace)
        if not audio.hangup.is_set():
            logger.info('Agent did not close in time; hanging up attempt=%s', attempt.id)
            return
        # A goodbye is playing (hangup_fallback owns it); check again in case it is interrupted.


async def handle_media(websocket, attempt):
    tasks = []
    reason = 'media_disconnected'
    try:
        call_mark(attempt, 'media_socket')
        logger.info('Media socket accepting attempt=%s', attempt.id)
        await websocket.accept()
        logger.info('Media socket accepted attempt=%s', attempt.id)
        async with asyncio.timeout(settings.MEDIA_START_TIMEOUT_SECONDS):
            while True:
                event = parse_message(await websocket.receive_text())
                logger.info('Media handshake event attempt=%s event=%s', attempt.id, event['event'])
                if event['event'] == 'start':
                    stream_id = validate_start(event, attempt)
                    call_mark(attempt, 'media_start')
                    break
                if event['event'] != 'connected':
                    raise ValueError('Expected stream start.')
        async def record_start():
            await runtime.start_stream(attempt.id, stream_id)
            logger.info('Media stream recorded attempt=%s stream=%s', attempt.id, stream_id)
            await asyncio.Future()  # Success must not end the audio bridge.

        start_task = asyncio.create_task(record_start(), name='stream_record')
        tasks = [start_task]
        logger.info('Media stream started attempt=%s stream=%s', attempt.id, stream_id)
        inbound = StreamingResampler(8000, 16000)
        # Dial already opened Gemini. A call that reaches media without that still starts here.
        audio = begin(attempt)
        await asyncio.wait_for(audio.ready.wait(), settings.MEDIA_START_TIMEOUT_SECONDS)
        if audio._task.done():
            audio._task.result()
        if audio.session is None:
            raise RuntimeError('Gemini session was not ready.')
        audio.stream_id = stream_id
        if audio.early:
            logger.info('Playing buffered greeting attempt=%s frames=%s', attempt.id, len(audio.early))

        async def writer():
            first_audio = True

            async def emit(epoch, message):
                nonlocal first_audio
                if epoch != audio.generation:
                    return
                if 'streamId' in message:
                    message = {**message, 'streamId': stream_id}
                await websocket.send_json(message)
                if message['event'] == 'playAudio':
                    if first_audio:
                        call_mark(attempt, 'first_audio_sent')
                        logger.info('First audio sent to Plivo attempt=%s', attempt.id)
                        first_audio = False
                    # Bound provider-side buffering and keep cancellation responsive.
                    await asyncio.sleep(len(base64.b64decode(message['media']['payload'])) / 16000)
                    audio.last_heard = audio.agent_played_at = time.monotonic()

            for epoch, message in await audio.take_early():
                await emit(epoch, message)
            while True:
                epoch, message = await audio.queue.get()
                await emit(epoch, message)

        async def receive_phone(session):
            while True:
                async with asyncio.timeout(settings.MEDIA_IDLE_TIMEOUT_SECONDS):
                    event = parse_message(await websocket.receive_text())
                if event.get('streamId', stream_id) != stream_id:
                    raise ValueError('Stream identity changed.')
                kind = event['event']
                if kind == 'media':
                    raw = decode_media(event)
                    await on_caller_audio(audio, raw, session)
                    pcm = inbound.convert(raw)
                    await audio.manager.send_audio_chunk(session, pcm)
                elif kind == 'stop':
                    return
                elif kind == 'playedStream':
                    if event.get('name') == HANGUP_CHECKPOINT and audio.hangup.is_set():
                        return  # The goodbye has finished playing.
                    # Only track our current checkpoints; arbitrary peer names cannot grow memory.
                    audio.played.discard(event.get('name'))
                elif kind in {'dtmf', 'clearedAudio'}:
                    continue
                elif kind == 'start':
                    raise ValueError('Duplicate stream start.')

        async def hangup_fallback():
            # If the playback acknowledgement never arrives, still hang up. A goodbye the caller
            # interrupts clears the hangup, so start waiting again.
            while True:
                await audio.hangup.wait()
                deadline = time.monotonic() + settings.CALL_HANGUP_GRACE_SECONDS
                while audio.hangup.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
                if audio.hangup.is_set():
                    return

        async def silence_watch():
            await watch_silence(audio, attempt)

        async def wrap_up():
            await wrap_up_after(audio, attempt, settings.CALL_WRAP_UP_SECONDS, settings.CALL_WRAP_UP_GRACE_SECONDS)

        async def monitor():
            while True:
                await asyncio.sleep(15)
                if not await runtime.heartbeat(attempt.id):
                    raise RuntimeError('Call authorization ended.')

        async with asyncio.timeout(settings.CALL_MAX_DURATION_SECONDS):
            tasks.extend(asyncio.create_task(fn(), name=name) for fn, name in [
                (writer, 'phone_writer'), (lambda: receive_phone(audio.session), 'phone_reader'),
                (monitor, 'call_monitor'), (hangup_fallback, 'hangup_fallback'),
                (silence_watch, 'silence_watch'), (wrap_up, 'wrap_up')])
            tasks.append(audio._task)
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if audio.hangup.is_set():
                    reason = 'agent_hangup'
                elif any(task.get_name() == 'silence_watch' for task in done):
                    reason = 'no_response'
                elif any(task.get_name() == 'wrap_up' for task in done):
                    reason = 'time_limit'
            finally:
                # Cancel pumps before closing the Gemini connection.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        reason = 'media_disconnected'
    except TimeoutError:
        reason = 'media_timeout'
        logger.warning('Media timeout attempt=%s', attempt.id)
    except asyncio.CancelledError:
        reason = 'server_shutdown'
        raise
    except Exception as exc:
        reason = 'bridge_failure'
        logger.warning('Bridge failed attempt=%s type=%s detail=%s', attempt.id, type(exc).__name__, exc)
    finally:
        logger.info('Media bridge closing attempt=%s reason=%s', attempt.id, reason)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(Exception):
            await websocket.close()
        await stop(attempt, force=True)
        # Persist cleanup before attempting external hangup. The worker retries failures.
        try:
            await terminate(attempt.id, reason)
        except Exception:
            logger.error('Cleanup deferred to reconciler attempt=%s', attempt.id)
