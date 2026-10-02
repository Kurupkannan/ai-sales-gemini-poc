"""Audio conversion utilities for Exotel (8 kHz) <-> Gemini Live (16 kHz / 24 kHz).

Handles:
- Exotel Inbound: 8 kHz, 16-bit signed PCM, mono, little-endian -> Gemini Inbound: 16 kHz, 16-bit signed PCM, mono
- Gemini Outbound: 24 kHz, 16-bit signed PCM, mono, little-endian -> Exotel Outbound: 8 kHz, 16-bit signed PCM, mono
- Base64 encoding and decoding for Exotel WebSocket payloads
"""

import base64
import logging
import numpy as np
import scipy.signal

logger = logging.getLogger(__name__)

INT16_MIN = -32768
INT16_MAX = 32767


def base64_to_pcm(payload: str) -> bytes:
    """Decodes a base64 string into raw PCM bytes."""
    if not payload:
        return b""
    try:
        return base64.b64decode(payload)
    except Exception as exc:
        logger.error("Failed to decode base64 audio payload: %s", exc)
        return b""


def pcm_to_base64(pcm_data: bytes) -> str:
    """Encodes raw PCM bytes into a base64 ASCII string."""
    if not pcm_data:
        return ""
    return base64.b64encode(pcm_data).decode("ascii")


def pcm8k_to_pcm16k(pcm_8k: bytes) -> bytes:
    """Resamples 8000 Hz 16-bit signed mono PCM to 16000 Hz 16-bit signed mono PCM.

    Ratio: 16000 / 8000 = 2 / 1 (up=2, down=1).
    """
    if not pcm_8k:
        return b""

    # Ensure 16-bit byte alignment (2 bytes per sample)
    if len(pcm_8k) % 2 != 0:
        pcm_8k = pcm_8k[: len(pcm_8k) - (len(pcm_8k) % 2)]

    if not pcm_8k:
        return b""

    try:
        # Interpret as little-endian 16-bit signed integers
        samples = np.frombuffer(pcm_8k, dtype=np.int16)
        if len(samples) == 0:
            return b""

        # Polyphase filter resample: up=2, down=1
        resampled = scipy.signal.resample_poly(samples, up=2, down=1)

        # Clip values to avoid overflow distortion and cast back to int16
        clipped = np.clip(resampled, INT16_MIN, INT16_MAX).astype(np.int16)
        return clipped.tobytes()
    except Exception as exc:
        logger.error("Error resampling 8k PCM to 16k: %s", exc)
        return b""


def pcm24k_to_pcm8k(pcm_24k: bytes) -> bytes:
    """Resamples 24000 Hz 16-bit signed mono PCM to 8000 Hz 16-bit signed mono PCM.

    Ratio: 8000 / 24000 = 1 / 3 (up=1, down=3).
    """
    if not pcm_24k:
        return b""

    # Ensure 16-bit byte alignment (2 bytes per sample)
    if len(pcm_24k) % 2 != 0:
        pcm_24k = pcm_24k[: len(pcm_24k) - (len(pcm_24k) % 2)]

    if not pcm_24k:
        return b""

    try:
        # Interpret as little-endian 16-bit signed integers
        samples = np.frombuffer(pcm_24k, dtype=np.int16)
        if len(samples) == 0:
            return b""

        # Polyphase filter resample: up=1, down=3
        resampled = scipy.signal.resample_poly(samples, up=1, down=3)

        # Clip values to avoid overflow distortion and cast back to int16
        clipped = np.clip(resampled, INT16_MIN, INT16_MAX).astype(np.int16)
        return clipped.tobytes()
    except Exception as exc:
        logger.error("Error resampling 24k PCM to 8k: %s", exc)
        return b""
