from __future__ import annotations
import struct
import zlib

MAGIC = b"CP"
VER = 1

TYPE_DATA = 0
TYPE_ACK = 1

_HDR_FMT = "!2sBBBBHHHH"  # magic(2), ver(1), type(1), rsv(1), rsv(1), msg_id, seq, total, length
# проще: фиксируем 2 резервных байта под будущее (окна, флаги)

def pack_frame(frame_type: int, msg_id: int, seq: int, total: int, payload: bytes) -> bytes:
    if not (0 <= msg_id <= 0xFFFF): raise ValueError("msg_id out of range")
    if not (0 <= seq <= 0xFFFF): raise ValueError("seq out of range")
    if not (0 <= total <= 0xFFFF): raise ValueError("total out of range")
    if len(payload) > 0xFFFF: raise ValueError("payload too large")

    header = struct.pack(_HDR_FMT, MAGIC, VER, frame_type, 0, 0, msg_id, seq, total, len(payload))
    body = header + payload
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack("!I", crc)

def unpack_frame(raw: bytes):
    if len(raw) < struct.calcsize(_HDR_FMT) + 4:
        raise ValueError("frame too short")

    header = raw[:struct.calcsize(_HDR_FMT)]
    magic, ver, frame_type, _r1, _r2, msg_id, seq, total, length = struct.unpack(_HDR_FMT, header)

    if magic != MAGIC: raise ValueError("bad magic")
    if ver != VER: raise ValueError("bad version")

    payload_start = struct.calcsize(_HDR_FMT)
    payload_end = payload_start + length
    if payload_end + 4 > len(raw):
        raise ValueError("bad length")

    payload = raw[payload_start:payload_end]
    crc_expected = struct.unpack("!I", raw[payload_end:payload_end + 4])[0]
    crc_actual = zlib.crc32(raw[:payload_end]) & 0xFFFFFFFF
    if crc_actual != crc_expected:
        raise ValueError("bad crc")

    return frame_type, msg_id, seq, total, payload

def fragment_message(payload: bytes, *, msg_id: int, max_payload: int = 32) -> list[bytes]:
    """
    Режем payload на несколько DATA-кадров.
    max_payload — сколько байт полезной нагрузки помещаем в один кадр.
    Возвращает список raw frames (bytes), которые потом можно передавать через PHY.
    """
    if max_payload <= 0:
        raise ValueError("max_payload must be > 0")

    total = (len(payload) + max_payload - 1) // max_payload
    if total == 0:
        total = 1  # пустое сообщение тоже можно передать одним кадром

    frames: list[bytes] = []
    for seq in range(total):
        part = payload[seq * max_payload:(seq + 1) * max_payload]
        frames.append(pack_frame(TYPE_DATA, msg_id=msg_id, seq=seq, total=total, payload=part))
    return frames


def reassemble_frames(frames_payloads: dict[int, bytes], total: int) -> bytes | None:
    """
    Собираем сообщение из частей.
    frames_payloads: {seq -> payload_part}
    total: ожидаемое число частей
    Возвращает bytes, если собралось всё, иначе None.
    """
    if total <= 0:
        raise ValueError("total must be > 0")

    if len(frames_payloads) < total:
        return None

    parts = []
    for seq in range(total):
        if seq not in frames_payloads:
            return None
        parts.append(frames_payloads[seq])

    return b"".join(parts)

def pack_ack(msg_id: int, seq: int, total: int) -> bytes:
    # payload пустой
    return pack_frame(TYPE_ACK, msg_id=msg_id, seq=seq, total=total, payload=b"")

def is_ack(frame_type: int) -> bool:
    return frame_type == TYPE_ACK
