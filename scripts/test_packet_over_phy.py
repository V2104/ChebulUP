from __future__ import annotations

import base64
import ggwave

from scripts.ggwave_codec import encoded_bytes_to_f32
from scripts.baseline_ggwave_file import init_rx, decode_two_ways, SR, PROTOCOL_ID
from scripts.packet import pack_frame, unpack_frame, TYPE_DATA


def main():
    inst_rx = init_rx()
    try:
        raw_frame = pack_frame(TYPE_DATA, msg_id=1, seq=0, total=1, payload=b"hello")

        # bytes -> base64 ascii str (ggwave.encode ждёт str)
        b64_text = base64.b64encode(raw_frame).decode("ascii")

        encoded = ggwave.encode(b64_text, protocolId=PROTOCOL_ID, volume=10)
        samples = encoded_bytes_to_f32(encoded)

        decoded = decode_two_ways(inst_rx, samples)
        print("Decoded raw:", decoded)

        if decoded is None:
            raise RuntimeError("decode returned None")

        # decoded is bytes of utf-8/ascii text => base64 decode back to raw_frame bytes
        decoded_text = decoded.decode("ascii")
        raw_back = base64.b64decode(decoded_text)

        ft, msg_id, seq, total, payload = unpack_frame(raw_back)
        print("Parsed:", ft, msg_id, seq, total, payload)

    finally:
        if hasattr(ggwave, "free"):
            try:
                ggwave.free(inst_rx)
            except Exception:
                pass


if __name__ == "__main__":
    main()
