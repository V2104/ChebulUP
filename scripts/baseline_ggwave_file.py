from __future__ import annotations

import numpy as np
import ggwave
from scripts.ggwave_codec import encoded_bytes_to_f32

SR = 48000
PROTOCOL_ID = 0


def decode_two_ways(inst_rx, samples_f32: np.ndarray) -> bytes | None:
    if samples_f32.dtype != np.float32:
        samples_f32 = samples_f32.astype(np.float32, copy=False)

    # 1) ndarray
    try:
        d = ggwave.decode(inst_rx, samples_f32)
        if d:
            return d
    except Exception:
        pass

    # 2) bytes (one-shot)
    try:
        d = ggwave.decode(inst_rx, samples_f32.tobytes())
        if d:
            return d
    except Exception:
        pass

    # 3) bytes streaming
    chunk = int(SR * 0.02)  # 20ms
    for i in range(0, len(samples_f32), chunk):
        part = samples_f32[i:i + chunk]
        try:
            d = ggwave.decode(inst_rx, part.tobytes())
            if d:
                return d
        except Exception:
            pass

    return None


def init_rx():
    # В твоей сборке это решает всё.
    params = ggwave.getDefaultParameters()
    if hasattr(params, "sampleRateInp"):
        params.sampleRateInp = float(SR)
    if hasattr(params, "sampleRateOut"):
        params.sampleRateOut = float(SR)
    return ggwave.init(params)


def main() -> int:
    msg = "abc"
    inst_rx = None
    try:
        inst_rx = init_rx()

        encoded = ggwave.encode(msg, protocolId=PROTOCOL_ID, volume=10)
        samples_f32 = encoded_bytes_to_f32(encoded)

        decoded = decode_two_ways(inst_rx, samples_f32)

        print(f"protocolId={PROTOCOL_ID}")
        print("Decoded:", decoded)

        ok = (decoded == msg.encode("utf-8"))
        print("OK" if ok else "FAIL")
        return 0 if ok else 1

    finally:
        if inst_rx is not None and hasattr(ggwave, "free"):
            try:
                ggwave.free(inst_rx)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
