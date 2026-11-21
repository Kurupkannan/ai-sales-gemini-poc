"""Audio conversion utilities for Plivo (8 kHz) <-> Gemini Live (16 kHz / 24 kHz).

Handles:
- Plivo Inbound: 8 kHz, 16-bit signed PCM, mono, little-endian -> Gemini Inbound: 16 kHz, 16-bit signed PCM, mono
- Gemini Outbound: 24 kHz, 16-bit signed PCM, mono, little-endian -> Plivo Outbound: 8 kHz, 16-bit signed PCM, mono
- Base64 encoding and decoding for Plivo WebSocket payloads
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
        samples = np.frombuffer(pcm_8k, dtype="<i2")
        if len(samples) == 0:
            return b""

        # Polyphase filter resample: up=2, down=1
        resampled = scipy.signal.resample_poly(samples.astype(np.float64), up=2, down=1)

        # Clip values to avoid overflow distortion and cast back to int16
        clipped = np.clip(resampled, INT16_MIN, INT16_MAX).astype("<i2")
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
        samples = np.frombuffer(pcm_24k, dtype="<i2")
        if len(samples) == 0:
            return b""

        # Polyphase filter resample: up=1, down=3
        resampled = scipy.signal.resample_poly(samples.astype(np.float64), up=1, down=3)

        # Clip values to avoid overflow distortion and cast back to int16
        clipped = np.clip(resampled, INT16_MIN, INT16_MAX).astype("<i2")
        return clipped.tobytes()
    except Exception as exc:
        logger.error("Error resampling 24k PCM to 8k: %s", exc)
        return b""


class StreamingResampler:
    """Stateful causal FIR resampling; preserves filter history across PCM frames."""

    def __init__(self, source_rate: int, target_rate: int):
        import math
        divisor = math.gcd(source_rate, target_rate)
        self.up, self.down = target_rate // divisor, source_rate // divisor
        factor = max(self.up, self.down)
        self.taps = scipy.signal.firwin(20 * factor + 1, 1 / factor) * self.up
        self.history = np.zeros(len(self.taps) - 1)
        self.phase = 0
        self.carry = b""

    def convert(self, pcm: bytes) -> bytes:
        pcm = self.carry + pcm
        self.carry = pcm[len(pcm) - len(pcm) % 2:] if len(pcm) % 2 else b""
        pcm = pcm[:len(pcm) - len(pcm) % 2]
        if not pcm:
            return b""
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        expanded = np.zeros(len(samples) * self.up)
        expanded[::self.up] = samples
        filtered, self.history = scipy.signal.lfilter(self.taps, [1.0], expanded, zi=self.history)
        result = filtered[(-self.phase) % self.down::self.down]
        self.phase = (self.phase + len(filtered)) % self.down
        return np.clip(np.rint(result), INT16_MIN, INT16_MAX).astype("<i2").tobytes()
