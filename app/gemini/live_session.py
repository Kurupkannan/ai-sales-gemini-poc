"""Gemini Live API connection and session management."""

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


def build_live_connect_config(
    system_instruction: str,
    voice_name: str = "Kore",
) -> types.LiveConnectConfig:
    """Builds the LiveConnectConfig for Gemini Live bidirectional audio session."""
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(
            parts=[types.Part.from_text(text=system_instruction)]
        ),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice_name
                )
            )
        ),
    )


class GeminiLiveManager:
    """Manages Gemini Live client connections and real-time audio interaction."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.8-live",
        system_instruction: str = "",
        voice_name: str = "Kore",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.system_instruction = system_instruction
        self.voice_name = voice_name
        self._client: genai.Client | None = None

    @property
    def client(self) -> genai.Client:
        """Lazily initializes the Gemini API client."""
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
        """Establishes a Live bidirectional session with Gemini."""
        config = build_live_connect_config(
            system_instruction=self.system_instruction,
            voice_name=self.voice_name,
        )

        logger.info("Opening Gemini Live session with model: %s", self.model)
        try:
            async with self.client.aio.live.connect(model=self.model, config=config) as session:
                logger.info("Gemini session started")
                try:
                    yield session
                finally:
                    logger.info("Gemini session closed")
        except Exception as exc:
            logger.error("Gemini Live connection error: %s", exc)
            raise

    async def send_greeting(self, session: any, greeting: str) -> None:
        """Sends the initial greeting turn to trigger the model's opening spoken line."""
        if not greeting:
            return
        logger.info("Sending initial greeting prompt to Gemini Live: %s", greeting)
        try:
            prompt = (
                f"[SYSTEM INSTRUCTION: The customer has just picked up the phone. "
                f"Speak your exact opening greeting line to the customer now: '{greeting}']"
            )
            await session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=prompt)],
                ),
                turn_complete=True,
            )
            logger.info("Successfully sent initial greeting prompt to Gemini Live")
        except Exception as exc:
            logger.error("Error sending initial greeting to Gemini Live: %s", exc, exc_info=True)
            raise

    async def send_audio_chunk(self, session: any, pcm_16k: bytes) -> None:
        """Sends a 16 kHz 16-bit mono PCM audio chunk to Gemini Live."""
        if not pcm_16k:
            return
        await session.send_realtime_input(
            audio=types.Blob(
                data=pcm_16k,
                mime_type="audio/pcm;rate=16000",
            )
        )
