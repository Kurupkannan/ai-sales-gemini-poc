"""Exotel AgentStream WebSocket protocol handling and audio bridge with Gemini Live."""

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from google.genai import types

from app.audio.converter import (
    base64_to_pcm,
    pcm24k_to_pcm8k,
    pcm8k_to_pcm16k,
    pcm_to_base64,
)
from app.audio.tts_engine import synthesize_text_to_pcm8k
from app.config import settings
from app.gemini.live_session import GeminiLiveManager

logger = logging.getLogger(__name__)


class ExotelMediaFormat(BaseModel):
    encoding: Optional[str] = None
    sample_rate: Optional[int] = 8000
    channels: Optional[int] = 1


class ExotelStartData(BaseModel):
    account_sid: Optional[str] = None
    stream_sid: Optional[str] = None
    call_sid: Optional[str] = None
    tracks: Optional[list[str]] = None
    media_format: Optional[ExotelMediaFormat] = None


class ExotelMediaData(BaseModel):
    track: Optional[str] = None
    chunk: Optional[str] = None
    timestamp: Optional[str] = None
    payload: Optional[str] = None


class ExotelStopData(BaseModel):
    account_sid: Optional[str] = None
    call_sid: Optional[str] = None


class ExotelEvent(BaseModel):
    event: str
    sequence_number: Optional[str] = None
    stream_sid: Optional[str] = None
    start: Optional[ExotelStartData] = None
    media: Optional[ExotelMediaData] = None
    stop: Optional[ExotelStopData] = None


def parse_exotel_message(raw_text: str) -> Optional[ExotelEvent]:
    """Parses incoming Exotel WebSocket JSON messages into an ExotelEvent model."""
    try:
        data = json.loads(raw_text)
        return ExotelEvent.model_validate(data)
    except Exception as exc:
        logger.warning("Failed to parse Exotel message: %s", exc)
        return None


def create_exotel_media_message(stream_sid: str, payload_b64: str) -> str:
    """Creates the Exotel outbound media event containing base64 raw 8 kHz PCM."""
    message = {
        "event": "media",
        "stream_sid": stream_sid,
        "media": {
            "payload": payload_b64,
        },
    }
    return json.dumps(message)


async def handle_exotel_websocket(websocket: WebSocket) -> None:
    """Handles an Exotel AgentStream WebSocket connection end-to-end for multi-turn conversation."""
    await websocket.accept()
    logger.info("[CALL_STARTED] Exotel WebSocket connection accepted")

    stream_sid: Optional[str] = None
    call_sid: Optional[str] = None

    # Step 1: Wait for initial handshake events: connected and start
    try:
        while True:
            raw_text = await websocket.receive_text()
            event = parse_exotel_message(raw_text)
            if not event:
                continue

            if event.event == "connected":
                logger.info("[EXOTEL_CONNECTED] Exotel connected event acknowledged")
                continue

            if event.event == "start":
                if event.start:
                    stream_sid = event.start.stream_sid or event.stream_sid
                    call_sid = event.start.call_sid
                else:
                    stream_sid = event.stream_sid

                logger.info("[EXOTEL_CONNECTED] Handshake complete. CallSid: %s, StreamSid: %s", call_sid, stream_sid)
                break

            if event.event == "stop":
                logger.info("[CALL_ENDED] Exotel sent stop event before stream start")
                return

    except WebSocketDisconnect as exc:
        logger.info("[CALL_ENDED] WebSocket disconnected before stream start (code=%s, reason=%s)", exc.code, exc.reason)
        return
    except Exception as exc:
        logger.error("Error during Exotel handshake: %s", exc, exc_info=True)
        return

    if not stream_sid:
        logger.error("Missing stream_sid from Exotel start event; closing connection")
        await websocket.close()
        return

    # Audio & turn counters
    exotel_audio_chunks_received = 0
    gemini_audio_chunks_sent = 0
    gemini_responses_received = 0
    gemini_audio_chunks_received = 0
    exotel_audio_chunks_sent = 0
    customer_audio_active = False
    gemini_response_active = False

    stop_event = asyncio.Event()

    gemini_manager = GeminiLiveManager(
        api_key=settings.GEMINI_API_KEY,
        model=settings.GEMINI_LIVE_MODEL,
        system_instruction=settings.SYSTEM_INSTRUCTION,
        voice_name=settings.VOICE_NAME,
    )

    async def _pump_exotel(gemini_session: Any) -> None:
        """Reads audio from Exotel, up-samples 8k -> 16k, and streams continuously to Gemini Live."""
        nonlocal exotel_audio_chunks_received, gemini_audio_chunks_sent, customer_audio_active
        try:
            while not stop_event.is_set():
                raw_text = await websocket.receive_text()
                event = parse_exotel_message(raw_text)
                if not event:
                    continue

                if event.event == "media" and event.media and event.media.payload:
                    pcm_8k = base64_to_pcm(event.media.payload)
                    if pcm_8k:
                        pcm_16k = pcm8k_to_pcm16k(pcm_8k)
                        await gemini_manager.send_audio_chunk(gemini_session, pcm_16k)
                        exotel_audio_chunks_received += 1
                        gemini_audio_chunks_sent += 1

                        if not customer_audio_active:
                            customer_audio_active = True
                            logger.info("[CUSTOMER_AUDIO_STARTED] First customer audio frame received from Exotel [AUDIO_CHUNK_RECEIVED]")

                        if exotel_audio_chunks_received % 100 == 0:
                            logger.info(
                                "[AUDIO_CHUNK_RECEIVED] Inbound chunks: %d from Exotel, %d sent to Gemini",
                                exotel_audio_chunks_received,
                                gemini_audio_chunks_sent,
                            )
                elif event.event == "stop":
                    logger.info("[EXOTEL_CONNECTED] Exotel sent 'stop' event for call_sid %s; call ending", call_sid)
                    stop_event.set()
                    break
                elif event.event == "dtmf":
                    logger.info("Exotel DTMF received")
                elif event.event == "mark":
                    logger.debug("Exotel mark event received")
        except WebSocketDisconnect as exc:
            logger.info("Exotel WebSocket disconnected during call: code=%s, reason=%s", exc.code, exc.reason)
            stop_event.set()
        except asyncio.CancelledError:
            logger.debug("pump_exotel task cancelled")
            raise
        except Exception as exc:
            logger.error("Error in pump_exotel: type=%s, message=%s", type(exc).__name__, exc, exc_info=True)
            stop_event.set()

    async def _pump_gemini(gemini_session: Any) -> None:
        """Receives Gemini Live audio across multiple turns, down-samples 24k -> 8k, and sends to Exotel."""
        nonlocal gemini_audio_chunks_received, exotel_audio_chunks_sent, gemini_responses_received, gemini_response_active
        try:
            while not stop_event.is_set():
                turn_received_any_message = False

                async for message in gemini_session.receive():
                    turn_received_any_message = True
                    if stop_event.is_set():
                        break

                    server_content = getattr(message, "server_content", None)
                    if not server_content:
                        if getattr(message, "go_away", None):
                            logger.warning("Gemini sent GoAway message: %s", message.go_away)
                            stop_event.set()
                            break
                        continue

                    # Interruption / Barge-in detection
                    if getattr(server_content, "interrupted", False):
                        logger.info("[INTERRUPTION_DETECTED] Gemini turn interrupted by customer speech (barge-in)")
                        if gemini_response_active:
                            gemini_response_active = False
                            logger.info("[GEMINI_RESPONSE_STOPPED] Response output interrupted by customer speech")

                    # Turn completion detection
                    if getattr(server_content, "turn_complete", False):
                        gemini_responses_received += 1
                        if gemini_response_active:
                            gemini_response_active = False
                            logger.info("[GEMINI_RESPONSE_STOPPED] Gemini turn completed. Total responses: %d", gemini_responses_received)

                    model_turn = getattr(server_content, "model_turn", None)
                    if not model_turn or not getattr(model_turn, "parts", None):
                        continue

                    for part in model_turn.parts:
                        inline_data = getattr(part, "inline_data", None)
                        if inline_data and getattr(inline_data, "data", None):
                            pcm_24k = inline_data.data
                            gemini_audio_chunks_received += 1

                            if not gemini_response_active:
                                gemini_response_active = True
                                logger.info("[GEMINI_RESPONSE_STARTED] Gemini started audio response output [AUDIO_CHUNK_SENT]")

                            pcm_8k = pcm24k_to_pcm8k(pcm_24k)
                            if pcm_8k:
                                b64_payload = pcm_to_base64(pcm_8k)
                                out_msg = create_exotel_media_message(stream_sid, b64_payload)
                                await websocket.send_text(out_msg)

                                if exotel_audio_chunks_sent == 0:
                                    logger.info("[AUDIO_CHUNK_SENT] Sent initial opening audio frame from Gemini to Exotel")
                                exotel_audio_chunks_sent += 1

                if not turn_received_any_message and not stop_event.is_set():
                    logger.info("Gemini receive stream ended with no further messages")
                    stop_event.set()
                    break
        except WebSocketDisconnect as exc:
            logger.info("Exotel WebSocket disconnected (outbound pump): code=%s, reason=%s", exc.code, exc.reason)
            stop_event.set()
        except asyncio.CancelledError:
            logger.debug("pump_gemini task cancelled")
            raise
        except Exception as exc:
            logger.error(
                "Gemini receive error: type=%s, message=%s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            stop_event.set()

    try:
        async with gemini_manager.connect() as gemini_session:
            logger.info("[GEMINI_CONNECTED] Connected to Gemini Live API session using model: %s", settings.GEMINI_LIVE_MODEL)
            # Step 2: Launch bidirectional audio pumps concurrently
            exotel_task = asyncio.create_task(_pump_exotel(gemini_session), name="pump_exotel")
            gemini_task = asyncio.create_task(_pump_gemini(gemini_session), name="pump_gemini")
            stop_task = asyncio.create_task(stop_event.wait(), name="stop_watcher")

            # Step 3: Trigger the opening Malayalam greeting prompt
            if settings.INITIAL_GREETING:
                async def _trigger_greeting():
                    try:
                        await gemini_manager.send_greeting(
                            session=gemini_session,
                            greeting=settings.INITIAL_GREETING,
                        )
                    except Exception as exc:
                        logger.error("Failed to send initial greeting prompt: %s", exc, exc_info=True)

                asyncio.create_task(_trigger_greeting(), name="send_initial_greeting")

            # Wait until stop_event is set or one of the pump tasks finishes
            done, pending = await asyncio.wait(
                [stop_task, exotel_task, gemini_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Ensure stop_event is set so all loops exit
            stop_event.set()

            # Inspect and log reason for task completion
            for task in done:
                if not task.cancelled():
                    exc = task.exception()
                    if exc:
                        logger.error(
                            "Task %s exited with exception: %s: %s",
                            task.get_name(),
                            type(exc).__name__,
                            exc,
                        )
                    else:
                        logger.info("Task %s completed normally", task.get_name())

            # Cancel remaining pending tasks
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    except Exception as exc:
        logger.error(
            "Error during active call session: type=%s, message=%s",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
    finally:
        logger.info(
            "[CALL_ENDED] Call ended. Final telemetry: "
            "exotel_audio_chunks_received=%d, "
            "gemini_audio_chunks_sent=%d, "
            "gemini_responses_received=%d, "
            "gemini_audio_chunks_received=%d, "
            "exotel_audio_chunks_sent=%d",
            exotel_audio_chunks_received,
            gemini_audio_chunks_sent,
            gemini_responses_received,
            gemini_audio_chunks_received,
            exotel_audio_chunks_sent,
        )
        try:
            await websocket.close()
        except Exception:
            pass
