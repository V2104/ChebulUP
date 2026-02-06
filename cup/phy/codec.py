from __future__ import annotations

import numpy as np
import ggwave


def bytes_to_float32_pcm(pcm_bytes: bytes) -> np.ndarray:
    a = np.frombuffer(pcm_bytes, dtype=np.float32)
    if a.size == 0:
        return np.array([], dtype=np.float32)
    if not np.isfinite(a).all():
        return np.array([], dtype=np.float32)
    if float(np.max(np.abs(a))) > 10.0:
        return np.array([], dtype=np.float32)
    return a


def i16_to_f32_pcm(samples_i16: np.ndarray) -> np.ndarray:
    return (samples_i16.astype(np.float32) / 32768.0)


def encoded_bytes_to_f32(encoded: bytes) -> np.ndarray:
    f32 = bytes_to_float32_pcm(encoded)
    if f32.size != 0:
        return f32
    i16 = np.frombuffer(encoded, dtype=np.int16)
    return i16_to_f32_pcm(i16)


def decode_stream(
    inst_rx,
    samples_f32: np.ndarray,
    *,
    sample_rate: int = 48000,
    chunk_ms: int = 20,
) -> bytes | None:
    if samples_f32.dtype != np.float32:
        samples_f32 = samples_f32.astype(np.float32, copy=False)

    chunk = max(1, int(sample_rate * (chunk_ms / 1000.0)))

    for i in range(0, len(samples_f32), chunk):
        part = samples_f32[i:i + chunk]
        decoded = ggwave.decode(inst_rx, part.tobytes())
        if decoded:
            return decoded

    return None
