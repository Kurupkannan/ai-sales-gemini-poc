"""Tests for audio conversion and base64 operations."""

import numpy as np
from app.audio.converter import (
    base64_to_pcm,
    pcm24k_to_pcm8k,
    pcm8k_to_pcm16k,
    pcm_to_base64,
)


def test_base64_pcm_roundtrip():
    original_pcm = b"\x00\x01\x02\x03\x04\x05\xfe\xff"
    b64_str = pcm_to_base64(original_pcm)
    assert isinstance(b64_str, str)
    decoded_pcm = base64_to_pcm(b64_str)
    assert decoded_pcm == original_pcm


def test_base64_empty_and_invalid():
    assert base64_to_pcm("") == b""
    assert pcm_to_base64(b"") == ""
    assert base64_to_pcm("not-valid-base64!!!") == b""


def test_pcm8k_to_pcm16k_resampling():
    # 8000 Hz, 160 samples (20ms) = 320 bytes (2 bytes per sample, 16-bit mono)
    sample_count = 160
    pcm_8k = np.zeros(sample_count, dtype=np.int16).tobytes()
    assert len(pcm_8k) == 320

    resampled_16k = pcm8k_to_pcm16k(pcm_8k)
    # Ratio is 2:1, so output should have 320 samples = 640 bytes
    assert len(resampled_16k) == 640


def test_pcm24k_to_pcm8k_resampling():
    # 24000 Hz, 480 samples (20ms) = 960 bytes (2 bytes per sample, 16-bit mono)
    sample_count = 480
    pcm_24k = np.zeros(sample_count, dtype=np.int16).tobytes()
    assert len(pcm_24k) == 960

    resampled_8k = pcm24k_to_pcm8k(pcm_24k)
    # Ratio is 1:3, so output should have 160 samples = 320 bytes
    assert len(resampled_8k) == 320


def test_pcm_odd_length_handling():
    # When odd byte length is received (unaligned int16), trailing byte is trimmed safely
    odd_8k = b"\x00\x01\x02"  # 3 bytes
    resampled_16k = pcm8k_to_pcm16k(odd_8k)
    # 2 bytes = 1 sample -> 2 samples = 4 bytes
    assert len(resampled_16k) == 4

    odd_24k = b"\x00\x01\x02\x03\x04\x05\x06"  # 7 bytes -> 6 bytes (3 samples) -> 1 sample = 2 bytes
    resampled_8k = pcm24k_to_pcm8k(odd_24k)
    assert len(resampled_8k) == 2


def test_pcm_empty_handling():
    assert pcm8k_to_pcm16k(b"") == b""
    assert pcm24k_to_pcm8k(b"") == b""


def test_non_silent_resampling_preserves_amplitude_and_pitch():
    from app.audio.converter import StreamingResampler
    frequency, rate = 440, 24000
    waveform = np.rint(12000*np.sin(2*np.pi*frequency*np.arange(rate)/rate)).astype('<i2')
    result = np.frombuffer(pcm24k_to_pcm8k(waveform.tobytes()), dtype='<i2')
    assert np.max(np.abs(result)) > 11000
    dominant = np.argmax(np.abs(np.fft.rfft(result)))
    assert abs(dominant-frequency) <= 1
    stream = StreamingResampler(24000, 8000)
    chunks = [waveform[i:i+477].tobytes() for i in range(0, len(waveform), 477)]
    streamed = b''.join(stream.convert(chunk) for chunk in chunks)
    whole = StreamingResampler(24000, 8000).convert(waveform.tobytes())
    np.testing.assert_array_equal(np.frombuffer(streamed, dtype='<i2'), np.frombuffer(whole, dtype='<i2'))
    assert max(np.frombuffer(streamed, dtype='<i2')) > 11000
