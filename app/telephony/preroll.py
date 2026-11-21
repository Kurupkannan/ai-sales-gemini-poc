"""Open Gemini and speak the greeting while the callee's phone is still ringing.

The media socket does not exist until after they pick up. Audio generated during
ringing is kept and flushed on the first playback frame, so the opening line
starts with the call instead of after a fresh Live handshake.
"""
import asyncio
import re
import time
import logging
from collections import deque
from contextlib import suppress

from app.audio.converter import StreamingResampler
from app.config import settings
from app.timing import call_mark
from app.gemini.agent_prompt import call_prompt, greeting_for
from app.gemini.live_session import END_CALL_TOOL, GeminiLiveManager
from app.plivo.websocket import play_audio

logger = logging.getLogger(__name__)
HANGUP_CHECKPOINT = 'end-call'
STAGE_TAGS = re.compile(r'<[^>]{0,40}>|\{[^}]{0,40}\}')


async def cancel_hangup(audio, session, *, stop_playback):
    """The caller cut into the goodbye: keep the call open and let the agent respond."""
    if not audio.hangup.is_set():
        return
    audio.hangup.clear()
    if stop_playback:
        generation = await audio.interrupt()
        if generation is not None:
            await audio.push(generation, {'event': 'clearAudio', 'streamId': audio.stream_id})
    call_id, audio.end_call_id = audio.end_call_id, None
    await audio.manager.send_tool_result(session, call_id, END_CALL_TOOL, {
        'result': 'cancelled: the customer interrupted your goodbye. Listen and reply to what they '
                  'said, then close with the matching line and call end_call again.'})


class PreparedAudio:
    def __init__(self):
        self.session = None
        self.manager = None
        self.ready = asyncio.Event()
        self.first_audio = asyncio.Event()
        self.attached = False
        self.stream_id = None
        self.generation = 0
        self.turn = 0
        self.played = set()
        self.outbound = StreamingResampler(24000, 8000)
        self.early = deque()
        self.queue = asyncio.Queue(maxsize=200)  # Four seconds once the phone is listening.
        self.buffered = False
        self.hangup = asyncio.Event()  # Set when the agent asks to end the call.
        # Last time anyone was heard: caller speech (transcribed) or agent audio played.
        self.last_heard = time.monotonic()
        self.caller_turns = 0
        self.agent_said = ''  # What the agent has said since the caller last spoke.
        self.caller_said = ''  # Caller's latest words (for the no-reply nudge).
        self.caller_spoke_at = 0.0
        self.agent_audio_at = 0.0  # When the model last produced audio.
        self.end_call_id = None
        self.agent_played_at = 0.0  # Last agent audio frame sent to the phone.
        self.caller_voice_at = 0.0  # Last loud caller frame (voice activity).
        self.voice_started = None  # Start of the caller's current loud stretch.
        self.barged = False  # Pending end_call, answered if the goodbye is interrupted.
        self._task = None
        self._lock = asyncio.Lock()

    async def push(self, epoch, message):
        async with self._lock:
            if epoch != self.generation:
                return
            if not self.attached:
                self.early.append((epoch, message))
                return
        await self.queue.put((epoch, message))

    async def take_early(self):
        async with self._lock:
            self.attached = True
            items = list(self.early)
            self.early.clear()
            return items

    async def interrupt(self):
        async with self._lock:
            self.generation += 1
            generation = self.generation
            self.outbound = StreamingResampler(24000, 8000)
            self.played.clear()
            if not self.attached:
                self.early.clear()
                return None
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        return generation


def begin(call):
    """Start one Live session for this attempt. Safe to call again."""
    audio = getattr(call, 'audio', None)
    if audio is not None:
        return audio
    audio = PreparedAudio()
    call.audio = audio
    audio._task = asyncio.create_task(_run(call, audio), name=f'gemini-preroll-{call.id}')
    def completed(task):
        if not task.cancelled() and task.exception() is not None:
            logger.warning('Gemini preroll failed attempt=%s type=%s', call.id, type(task.exception()).__name__)
    audio._task.add_done_callback(completed)
    logger.info('Gemini preroll started attempt=%s', call.id)
    return audio


async def stop(call, *, force=False):
    audio = getattr(call, 'audio', None)
    if audio is None or (audio.attached and not force):
        return
    task = audio._task
    if task and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def wait_for_greeting(call):
    """Do not ring a customer until actual greeting PCM has been buffered."""
    audio = begin(call)
    if not settings.INITIAL_GREETING.strip():
        raise RuntimeError('An opening greeting is required before dialing.')
    waiter = asyncio.create_task(audio.first_audio.wait())
    try:
        async with asyncio.timeout(settings.CALL_GREETING_PREPARE_TIMEOUT_SECONDS):
            done, _ = await asyncio.wait({waiter, audio._task}, return_when=asyncio.FIRST_COMPLETED)
            if audio._task in done:
                audio._task.result()
                raise RuntimeError('Gemini ended before dialing.')
            if not audio.early:
                raise RuntimeError('Greeting buffer is empty.')
            call_mark(call, 'greeting_ready')
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def _run(call, audio):
    manager = GeminiLiveManager(
        api_key=settings.GEMINI_API_KEY, model=settings.GEMINI_LIVE_MODEL,
        system_instruction=call_prompt(settings.SYSTEM_INSTRUCTION,
            getattr(call, 'lead_name', None), getattr(call, 'lead_source', None),
            getattr(call, 'preferred_language', None)),
        voice_name=settings.VOICE_NAME)
    audio.manager = manager
    try:
        call_mark(call, 'gemini_connect_start')
        async with manager.connect() as session:
            call_mark(call, 'gemini_connected')
            audio.session = session
            if settings.INITIAL_GREETING:
                await manager.send_greeting(session, greeting_for(
                    getattr(call, 'lead_name', None), settings.INITIAL_GREETING))
            call_mark(call, 'greeting_sent')
            audio.ready.set()
            await _pump(call, audio, session)
    finally:
        audio.ready.set()
        if manager._client is not None:
            await manager._client.aio.aclose()


async def _pump(call, audio, session):
    while True:
        received = False
        async for message in session.receive():
            received = True
            if getattr(message, 'go_away', None):
                raise RuntimeError('Gemini session ended.')
            tool_call = getattr(message, 'tool_call', None)
            for function_call in getattr(tool_call, 'function_calls', None) or []:
                if function_call.name == END_CALL_TOOL and not audio.hangup.is_set():
                    if not audio.agent_said.strip() or audio.agent_said.rstrip(' "\'”').endswith('?'):
                        # A call must end on a spoken goodbye, never right after a question.
                        logger.info('Refused premature hangup attempt=%s', call.id)
                        await audio.manager.send_tool_result(session, getattr(function_call, 'id', None), END_CALL_TOOL, {
                            'result': 'refused: you just asked a question or said no goodbye. Wait for the '
                                      'customer to answer, then say the closing line before calling end_call.'})
                        continue
                    reason = (getattr(function_call, 'args', None) or {}).get('reason')
                    logger.info('Agent requested hangup attempt=%s reason=%s', call.id, str(reason)[:50])
                    audio.hangup.set()
                    audio.end_call_id = getattr(function_call, 'id', None)
                    # Queued after the goodbye audio: Plivo acknowledges it once that has played.
                    await audio.push(audio.generation, {
                        'event': 'checkpoint', 'streamId': audio.stream_id, 'name': HANGUP_CHECKPOINT})
            content = getattr(message, 'server_content', None)
            if not content:
                continue
            for speaker, field_name in [('customer', 'input_transcription'), ('agent', 'output_transcription')]:
                item = getattr(content, field_name, None)
                text = STAGE_TAGS.sub('', getattr(item, 'text', None) or '') if item else ''
                if text.strip():
                    if speaker == 'customer':
                        audio.last_heard = audio.caller_spoke_at = time.monotonic()
                        audio.caller_turns += 1
                        audio.caller_said = text
                        audio.agent_said = ''
                        await cancel_hangup(audio, session, stop_playback=True)
                    else:
                        audio.agent_said += text
                    if hasattr(call, 'add_transcript'):
                        call.add_transcript(speaker, text)
            if getattr(content, 'interrupted', False):
                await cancel_hangup(audio, session, stop_playback=False)
                generation = await audio.interrupt()
                if generation is not None:
                    await audio.push(generation, {'event': 'clearAudio', 'streamId': audio.stream_id})
                continue  # Never play inline audio attached to an interrupted turn.
            model_turn = getattr(content, 'model_turn', None)
            for part in getattr(model_turn, 'parts', None) or []:
                inline = getattr(part, 'inline_data', None)
                if inline and getattr(inline, 'data', None):
                    call_mark(call, 'gemini_first_audio')
                    audio.agent_audio_at = time.monotonic()
                    pcm = audio.outbound.convert(inline.data)
                    for offset in range(0, len(pcm), 320):
                        await audio.push(audio.generation, play_audio(pcm[offset:offset + 320]))
                    if pcm:
                        audio.first_audio.set()
                    if not audio.attached and not audio.buffered and audio.early:
                        audio.buffered = True
                        logger.info('Greeting audio buffered before answer attempt=%s', call.id)
            if getattr(content, 'turn_complete', False):
                audio.turn += 1
                name = f'{audio.generation}-{audio.turn}'
                if len(audio.played) >= 100:
                    raise RuntimeError('Playback acknowledgements stalled.')
                audio.played.add(name)
                await audio.push(audio.generation, {
                    'event': 'checkpoint', 'streamId': audio.stream_id, 'name': name})
        if not received:
            return
        await asyncio.sleep(0)
