from __future__ import annotations

import base64
import random
import time
import ggwave

from scripts.baseline_ggwave_file import init_rx, decode_two_ways, SR, PROTOCOL_ID
from scripts.ggwave_codec import encoded_bytes_to_f32
from scripts.packet import (
    fragment_message, unpack_frame, reassemble_frames,
    pack_ack, TYPE_DATA, TYPE_ACK
)

# --- PHY helpers (строка через ggwave) ---

def phy_encode_text(text: str) -> bytes:
    return ggwave.encode(text, protocolId=PROTOCOL_ID, volume=10)

def phy_decode_text(inst_rx, samples_bytes: bytes) -> str | None:
    samples_f32 = encoded_bytes_to_f32(samples_bytes)
    decoded = decode_two_ways(inst_rx, samples_f32)
    if decoded is None:
        return None
    return decoded.decode("ascii", errors="strict")


def bytes_frame_to_text(frame: bytes) -> str:
    return base64.b64encode(frame).decode("ascii")

def text_to_bytes_frame(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# --- Simulated unreliable channel ---

class UnreliableChannel:
    def __init__(self, drop_prob: float = 0.2, delay_ms: int = 0):
        self.drop_prob = drop_prob
        self.delay_ms = delay_ms
        self.queue: list[bytes] = []

    def send(self, phy_samples: bytes):
        # drop
        if random.random() < self.drop_prob:
            return
        # delay (optional)
        if self.delay_ms > 0:
            time.sleep(self.delay_ms / 1000.0)
        self.queue.append(phy_samples)

    def recv(self) -> bytes | None:
        if not self.queue:
            return None
        return self.queue.pop(0)


# --- Stop-and-Wait ARQ over frames ---

def sender_send_message(channel_data: UnreliableChannel, channel_ack: UnreliableChannel, payload: bytes,
                        *, msg_id: int = 1, max_payload: int = 16, timeout_s: float = 1.0, max_retries: int = 10):
    frames = fragment_message(payload, msg_id=msg_id, max_payload=max_payload)

    inst_rx_ack = init_rx()
    try:
        for raw_frame in frames:
            # узнаем seq/total (чтобы ждать правильный ack)
            ft, mid, seq, total, _ = unpack_frame(raw_frame)

            if ft != TYPE_DATA:
                raise RuntimeError("expected DATA frame")

            text = bytes_frame_to_text(raw_frame)

            retries = 0
            while True:
                # отправляем DATA
                channel_data.send(phy_encode_text(text))

                # ждём ACK
                deadline = time.time() + timeout_s
                got_ack = False
                while time.time() < deadline:
                    ack_samples = channel_ack.recv()
                    if ack_samples is None:
                        time.sleep(0.01)
                        continue

                    ack_text = phy_decode_text(inst_rx_ack, ack_samples)
                    if ack_text is None:
                        continue

                    ack_frame = text_to_bytes_frame(ack_text)
                    aft, amid, aseq, atotal, _apayload = unpack_frame(ack_frame)
                    if aft == TYPE_ACK and amid == msg_id and aseq == seq and atotal == total:
                        got_ack = True
                        break

                if got_ack:
                    print(f"SENDER: seq={seq}/{total-1} ACK")
                    break

                retries += 1
                print(f"SENDER: seq={seq}/{total-1} timeout, retry {retries}/{max_retries}")
                if retries >= max_retries:
                    raise RuntimeError(f"SENDER failed: too many retries on seq={seq}")

    finally:
        if hasattr(ggwave, "free"):
            try:
                ggwave.free(inst_rx_ack)
            except Exception:
                pass


def receiver_run(channel_data: UnreliableChannel, channel_ack: UnreliableChannel,
                 *, msg_id: int = 1, grace_after_assembled_s: float = 2.0):
    inst_rx_data = init_rx()
    try:
        got_parts: dict[int, bytes] = {}
        expected_total: int | None = None

        assembled_data: bytes | None = None
        assembled_at: float | None = None

        while True:
            # Если уже собрали — работаем ещё чуть-чуть как "ACK-сервис", потом выходим
            if assembled_data is not None and assembled_at is not None:
                if time.time() - assembled_at >= grace_after_assembled_s:
                    return assembled_data

            samples = channel_data.recv()
            if samples is None:
                time.sleep(0.01)
                continue

            text = phy_decode_text(inst_rx_data, samples)
            if text is None:
                continue

            raw_frame = text_to_bytes_frame(text)
            ft, mid, seq, total, payload = unpack_frame(raw_frame)

            if ft != TYPE_DATA or mid != msg_id:
                continue

            # сохраняем часть (повторы допускаем)
            if expected_total is None:
                expected_total = total
            got_parts[seq] = payload

            # шлём ACK всегда (и на повторы тоже)
            ack = pack_ack(msg_id=mid, seq=seq, total=total)
            channel_ack.send(phy_encode_text(bytes_frame_to_text(ack)))
            print(f"RECV: got seq={seq}/{total-1}, sent ACK")

            # проверяем сборку
            assembled = reassemble_frames(got_parts, expected_total)
            if assembled is not None and assembled_data is None:
                assembled_data = assembled
                assembled_at = time.time()
                print(f"RECV: assembled len={len(assembled_data)}")

    finally:
        if hasattr(ggwave, "free"):
            try:
                ggwave.free(inst_rx_data)
            except Exception:
                pass


def main():
    random.seed(1)

    # Два канала: данные и ACK (оба ненадёжные)
    data_ch = UnreliableChannel(drop_prob=0.25)
    ack_ch = UnreliableChannel(drop_prob=0.10)

    payload = b"hello world! " * 10

    # Запускаем receiver в "ручном" режиме: в одном потоке будем шагать
    # (без threading, чтобы проще отлаживать)
    # Хитрость: сначала посылаем sender, а receiver будет забирать из очереди в процессе.
    #
    # Для простоты делаем так:
    # 1) sender пытается отправить кадр
    # 2) receiver крутится и отвечает ACK
    #
    # Реализуем это через interleave: sender вызывает send(), receiver тут же читаeт.

    # Чтобы не усложнять, делаем receiver в фоне через простую петлю,
    # а sender будет "подливать" данные.
    import threading
    result_holder = {"data": None, "err": None}

    def recv_thread():
        try:
            result_holder["data"] = receiver_run(data_ch, ack_ch, msg_id=1)
        except Exception as e:
            result_holder["err"] = e

    t = threading.Thread(target=recv_thread, daemon=True)
    t.start()

    sender_send_message(data_ch, ack_ch, payload, msg_id=1, max_payload=16, timeout_s=0.8, max_retries=20)

    t.join(timeout=10)
    if result_holder["err"]:
        raise result_holder["err"]

    received = result_holder["data"]
    print("FINAL:", "OK" if received == payload else "FAIL")


if __name__ == "__main__":
    main()
