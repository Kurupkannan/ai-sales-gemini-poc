"""Tests for Gemini Live configuration and session helpers (mocked external calls)."""

from unittest.mock import AsyncMock
import pytest
from google.genai import types

from app.gemini.live_session import (
    GeminiLiveManager,
    build_live_connect_config,
)


def test_build_live_connect_config():
    system_instruction = "You are a friendly voice assistant."
    voice = "Aoede"
    config = build_live_connect_config(system_instruction=system_instruction, voice_name=voice)

    assert config.response_modalities == [types.Modality.AUDIO]
    assert config.system_instruction is not None
    assert config.speech_config.voice_config.prebuilt_voice_config.voice_name == "Aoede"


def test_gemini_manager_initialization():
    manager = GeminiLiveManager(
        api_key="dummy_api_key",
        model="gemini-3.8-live",
        system_instruction="Hello instruct",
        voice_name="Puck",
    )
    assert manager.api_key == "dummy_api_key"
    assert manager.model == "gemini-3.8-live"
    assert manager.voice_name == "Puck"


@pytest.mark.asyncio
async def test_send_greeting():
    manager = GeminiLiveManager(api_key="dummy_key")
    mock_session = AsyncMock()
    greeting_text = "ഹലോ, നമസ്കാരം."

    await manager.send_greeting(mock_session, greeting_text)

    mock_session.send_client_content.assert_awaited_once()
    call_kwargs = mock_session.send_client_content.call_args.kwargs
    assert call_kwargs.get("turn_complete") is True
    turns = call_kwargs.get("turns")
    assert isinstance(turns, types.Content)
    assert greeting_text in turns.parts[0].text


@pytest.mark.asyncio
async def test_send_audio_chunk():
    manager = GeminiLiveManager(api_key="dummy_key")
    mock_session = AsyncMock()
    dummy_audio = b"\x00\x01\x02\x03"

    await manager.send_audio_chunk(mock_session, dummy_audio)

    mock_session.send_realtime_input.assert_awaited_once()
    call_kwargs = mock_session.send_realtime_input.call_args.kwargs
    audio_blob = call_kwargs.get("audio")
    assert isinstance(audio_blob, types.Blob)
    assert audio_blob.data == dummy_audio
    assert "audio/pcm" in audio_blob.mime_type
