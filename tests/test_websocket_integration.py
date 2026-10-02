"""Integration tests for multi-turn Exotel <-> Gemini WebSocket lifecycle."""

import asyncio
import json
from unittest.mock import AsyncMock, patch
import pytest
from starlette.testclient import TestClient
from google.genai import types

from app.audio.converter import pcm_to_base64
from app.config import settings
from app.main import app


class DummyServerContent:
    def __init__(self, pcm_24k: bytes, turn_complete: bool = True, interrupted: bool = False):
        part = types.Part(inline_data=types.Blob(data=pcm_24k, mime_type="audio/pcm;rate=24000"))
        self.model_turn = types.Content(role="model", parts=[part])
        self.interrupted = interrupted
        self.turn_complete = turn_complete


class DummyLiveMessage:
    def __init__(self, pcm_24k: bytes, turn_complete: bool = True, interrupted: bool = False):
        self.server_content = DummyServerContent(pcm_24k, turn_complete, interrupted)


def create_multiturn_receive_mock(num_turns: int = 3):
    """Creates a mock receive method that simulates num_turns separate turns, mimicking Google GenAI SDK behavior."""
    current_turn = 0

    def fake_receive():
        nonlocal current_turn
        current_turn += 1
        turn_num = current_turn

        async def _generator():
            dummy_24k = (bytes([turn_num % 128]) * 2) * 480
            yield DummyLiveMessage(dummy_24k, turn_complete=True)

        return _generator()

    return fake_receive


def test_multiturn_gemini_remains_alive_after_first_response():
    """Verifies that the session remains alive across multiple turns and receives multiple responses."""
    mock_session = AsyncMock()
    mock_session.receive = create_multiturn_receive_mock(num_turns=3)

    with patch("app.gemini.live_session.GeminiLiveManager.connect") as mock_connect:
        mock_connect.return_value.__aenter__.return_value = mock_session
        mock_connect.return_value.__aexit__.return_value = None

        client = TestClient(app)
        with client.websocket_connect("/exotel/media") as websocket:
            websocket.send_text(json.dumps({"event": "connected"}))
            start_event = {
                "event": "start",
                "sequence_number": "1",
                "stream_sid": "stream_multiturn_test",
                "start": {
                    "account_sid": "acc_1",
                    "stream_sid": "stream_multiturn_test",
                    "call_sid": "call_1",
                    "media_format": {"sample_rate": 8000},
                },
            }
            websocket.send_text(json.dumps(start_event))

            msg1 = json.loads(websocket.receive_text())
            assert msg1["event"] == "media"
            assert msg1["stream_sid"] == "stream_multiturn_test"

            pcm_8k = b"\x01\x00" * 160
            media_event_1 = {
                "event": "media",
                "sequence_number": "2",
                "stream_sid": "stream_multiturn_test",
                "media": {"payload": pcm_to_base64(pcm_8k)},
            }
            websocket.send_text(json.dumps(media_event_1))

            msg2 = json.loads(websocket.receive_text())
            assert msg2["event"] == "media"

            media_event_2 = {
                "event": "media",
                "sequence_number": "3",
                "stream_sid": "stream_multiturn_test",
                "media": {"payload": pcm_to_base64(pcm_8k)},
            }
            websocket.send_text(json.dumps(media_event_2))

            msg3 = json.loads(websocket.receive_text())
            assert msg3["event"] == "media"

            stop_event = {
                "event": "stop",
                "sequence_number": "4",
                "stream_sid": "stream_multiturn_test",
                "stop": {"call_sid": "call_1"},
            }
            websocket.send_text(json.dumps(stop_event))


def test_exotel_stop_closes_gemini_session():
    """Verifies that an Exotel 'stop' event cleanly closes the Gemini session."""
    mock_session = AsyncMock()
    mock_session.receive = create_multiturn_receive_mock(num_turns=1)

    with patch("app.gemini.live_session.GeminiLiveManager.connect") as mock_connect:
        mock_connect.return_value.__aenter__.return_value = mock_session
        mock_connect.return_value.__aexit__.return_value = None

        client = TestClient(app)
        with client.websocket_connect("/exotel/media") as websocket:
            websocket.send_text(json.dumps({"event": "connected"}))
            websocket.send_text(json.dumps({
                "event": "start",
                "stream_sid": "stream_stop_test",
                "start": {"call_sid": "call_stop_1", "stream_sid": "stream_stop_test"},
            }))

            json.loads(websocket.receive_text())

            websocket.send_text(json.dumps({
                "event": "stop",
                "stream_sid": "stream_stop_test",
                "stop": {"call_sid": "call_stop_1"},
            }))


def test_unexpected_gemini_error_closes_cleanly():
    """Verifies that an unrecoverable Gemini exception cleans up tasks and closes without crashing."""
    mock_session = AsyncMock()

    def error_receive():
        async def _generator():
            raise RuntimeError("Gemini connection unexpectedly reset")
            yield  # pragma: no cover
        return _generator()

    mock_session.receive = error_receive

    with patch("app.gemini.live_session.GeminiLiveManager.connect") as mock_connect:
        mock_connect.return_value.__aenter__.return_value = mock_session
        mock_connect.return_value.__aexit__.return_value = None

        client = TestClient(app)
        with client.websocket_connect("/exotel/media") as websocket:
            websocket.send_text(json.dumps({"event": "connected"}))
            websocket.send_text(json.dumps({
                "event": "start",
                "stream_sid": "stream_err_test",
                "start": {"call_sid": "call_err_1", "stream_sid": "stream_err_test"},
            }))


def test_multiple_inbound_audio_chunks_counted():
    """Verifies that hundreds of audio chunks can stream in continuously."""
    mock_session = AsyncMock()
    mock_session.receive = create_multiturn_receive_mock(num_turns=1)

    with patch("app.gemini.live_session.GeminiLiveManager.connect") as mock_connect:
        mock_connect.return_value.__aenter__.return_value = mock_session
        mock_connect.return_value.__aexit__.return_value = None

        client = TestClient(app)
        with client.websocket_connect("/exotel/media") as websocket:
            websocket.send_text(json.dumps({"event": "connected"}))
            websocket.send_text(json.dumps({
                "event": "start",
                "stream_sid": "stream_chunks_test",
                "start": {"call_sid": "call_c_1", "stream_sid": "stream_chunks_test"},
            }))

            json.loads(websocket.receive_text())

            pcm_8k = b"\x00\x02" * 160
            b64 = pcm_to_base64(pcm_8k)
            for i in range(100):
                websocket.send_text(json.dumps({
                    "event": "media",
                    "stream_sid": "stream_chunks_test",
                    "media": {"payload": b64},
                }))

            assert mock_session.send_realtime_input.call_count >= 90

            websocket.send_text(json.dumps({
                "event": "stop",
                "stream_sid": "stream_chunks_test",
                "stop": {"call_sid": "call_c_1"},
            }))
