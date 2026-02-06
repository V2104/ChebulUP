from __future__ import annotations

import base64
import ggwave

from scripts.baseline_ggwave_file import init_rx, decode_two_ways, SR, PROTOCOL_ID
from scripts.ggwave_codec import encoded_bytes_to_f32
from scripts.packet import fragment_message, unpack_frame, reassemble_frames, TYPE_DATA


def phy_send_text(frame_bytes: bytes) -> bytes:
    """
    bytes -> base64 str -> PHY -> decoded bytes (base64 text)
    """
    b64_text = base64.b64encode(frame_bytes).decode("ascii")
    encoded = ggwave.encode(b64_text, protocolId=PROTOCOL_ID, volume=10)
    samples = encoded_bytes_to_f32(encoded)
    return samples


def main():
    inst_rx = init_rx()
    try:
        msg_id = 42
        original = b"hello world! " * 10  # специально длиннее одного кадра

        frames = fragment_message(original, msg_id=msg_id, max_payload=16)
        print(f"Original len={len(original)} bytes, frames={len(frames)}")

        got_parts: dict[int, bytes] = {}
        expected_total = None

        for idx, raw_frame in enumerate(frames):
            # передаём кадр через PHY
            b64_text = base64.b64encode(raw_frame).decode("ascii")
            encoded = ggwave.encode(b64_text, protocolId=PROTOCOL_ID, volume=10)
            samples = encoded_bytes_to_f32(encoded)

            decoded = decode_two_ways(inst_rx, samples)
            if decoded is None:
                raise RuntimeError(f"decode returned None at frame {idx}")

            raw_back = base64.b64decode(decoded.decode("ascii"))

            frame_type, mid, seq, total, payload = unpack_frame(raw_back)

            if frame_type != TYPE_DATA:
                raise RuntimeError("unexpected frame type")

            if mid != msg_id:
                raise RuntimeError("unexpected msg_id")

            if expected_total is None:
                expected_total = total
            elif expected_total != total:
                raise RuntimeError("total mismatch")

            got_parts[seq] = payload
            assembled = reassemble_frames(got_parts, total)

            print(f"frame {idx}: seq={seq}/{total-1}, part={len(payload)} bytes, assembled={'yes' if assembled is not None else 'no'}")

        final = reassemble_frames(got_parts, expected_total or 0)
        print("Final len:", len(final) if final else None)
        print("OK" if final == original else "FAIL")

    finally:
        if hasattr(ggwave, "free"):
            try:
                ggwave.free(inst_rx)
            except Exception:
                pass


if __name__ == "__main__":
    main()
