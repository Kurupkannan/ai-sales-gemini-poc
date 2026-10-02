"""Neural Text-to-Speech Engine for ultra-realistic Malayalam voice synthesis."""

import logging
import miniaudio
import numpy as np
import scipy.signal
import edge_tts

logger = logging.getLogger(__name__)

DEFAULT_TTS_VOICE = "ml-IN-SobhanaNeural"  # Female Malayalam Neural Voice
MALE_TTS_VOICE = "ml-IN-MidhunNeural"     # Male Malayalam Neural Voice


async def synthesize_text_to_pcm8k(text: str, voice_name: str = DEFAULT_TTS_VOICE) -> bytes:
    """Synthesizes input text into 8000 Hz 16-bit signed mono PCM audio bytes for Exotel stream."""
    clean_text = text.strip() if text else ""
    if not clean_text:
        return b""

    try:
        communicate = edge_tts.Communicate(clean_text, voice_name)
        mp3_bytes = b""
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                mp3_bytes += chunk["data"]

        if not mp3_bytes:
            logger.warning("TTS returned empty audio data for text: %s", clean_text[:50])
            return b""

        decoded = miniaudio.decode(mp3_bytes)
        samples = np.frombuffer(decoded.samples, dtype=np.int16)

        if decoded.nchannels > 1:
            samples = samples.reshape(-1, decoded.nchannels).mean(axis=1).astype(np.int16)

        if decoded.sample_rate != 8000:
            gcd = np.gcd(8000, decoded.sample_rate)
            up = 8000 // gcd
            down = decoded.sample_rate // gcd
            resampled = scipy.signal.resample_poly(samples, up=up, down=down)
            clipped = np.clip(resampled, -32768, 32767).astype(np.int16)
            return clipped.tobytes()

        return samples.tobytes()
    except Exception as exc:
        logger.error("TTS synthesis error for voice '%s': %s", voice_name, exc, exc_info=True)
        return b""
