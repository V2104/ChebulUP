from __future__ import annotations

import base64
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass

import ggwave

from scripts.ggwave_codec import encoded_bytes_to_f32
from scripts.packet import (
    TYPE_ACK,
    TYPE_DATA,
    fragment_message,
    pack_ack,
    reassemble_frames,
    unpack_frame,
)
from scripts.config import SR, PROTOCOL_ID


_B64_ALPH = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


@contextmanager
def suppress_c_stdout_stderr():
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stdout = os.dup(1)
    old_stderr = os.dup(2)
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(old_stdout, 1)
        os.dup2(old_stderr, 2)
        os.close(old_stdout)
        os.close(old_stderr)
        os.close(devnull)


try:
    if hasattr(ggwave, "disableLog"):
        ggwave.disableLog()
except Exception:
    pass


def corrupt_b64_ascii(b: bytes) -> bytes:
    if not b:
        return b
    s = bytearray(b)
    i = random.randrange(len(s) - 1) if len(s) > 2 else 0
    if s[i] == ord("=") and len(s) > 1:
        i = max(0, i - 1)
    old = s[i]
    new = ord(_B64_ALPH[random.randrange(len(_B64_ALPH))])
    if new == old:
        new = ord(_B64_ALPH[(random.randrange(len(_B64_ALPH)) + 1) % len(_B64_ALPH)])
    s[i] = new
    return bytes(s)


def init_rx():
    params = ggwave.getDefaultParameters()
    if hasattr(params, "sampleRateInp"):
        params.sampleRateInp = float(SR)
    if hasattr(params, "sampleRateOut"):
        params.sampleRateOut = float(SR)
    return ggwave.init(params)


def free_rx(inst):
    if hasattr(ggwave, "free"):
        try:
            ggwave.free(inst)
        except Exception:
            pass


def phy_encode_text(text: str) -> bytes:
    with suppress_c_stdout_stderr():
        return ggwave.encode(text, protocolId=PROTOCOL_ID, volume=10)


def phy_decode_b64bytes(inst_rx, phy_samples: bytes) -> bytes | None:
    samples_f32 = encoded_bytes_to_f32(phy_samples)

    try:
        with suppress_c_stdout_stderr():
            d = ggwave.decode(inst_rx, samples_f32)
        if d:
            return d
    except Exception:
        pass

    try:
        with suppress_c_stdout_stderr():
            d = ggwave.decode(inst_rx, samples_f32.tobytes())
        if d:
            return d
    except Exception:
        pass

    return None


class UnreliableChannel:
    def __init__(self, drop_prob: float):
        self.drop_prob = float(drop_prob)
        self.queue: list[bytes] = []

    def send(self, item: bytes) -> bool:
        if random.random() < self.drop_prob:
            return False
        self.queue.append(item)
        return True

    def recv(self) -> bytes | None:
        if not self.queue:
            return None
        return self.queue.pop(0)


@dataclass
class RunResult:
    ok: bool
    seconds: float
    goodput_Bps: float
    frames_total: int
    timeouts_total: int
    crc_fail_total: int
    data_sent: int
    data_dropped: int
    ack_sent: int
    ack_dropped: int


def run_once(
    *,
    payload: bytes,
    drop_data: float,
    drop_ack: float,
    max_payload: int,
    timeout_s: float,
    max_retries: int,
    seed: int,
    msg_id: int = 1,
    window: int = 4,
    corrupt_data_prob: float = 0.0,
    corrupt_ack_prob: float = 0.0,
) -> RunResult:
    random.seed(seed)

    data_ch = UnreliableChannel(drop_data)
    ack_ch = UnreliableChannel(drop_ack)

    inst_rx_data = init_rx()
    inst_rx_ack = init_rx()

    counters = {
        "timeouts_total": 0,
        "crc_fail_total": 0,
        "data_sent": 0,
        "data_dropped": 0,
        "ack_sent": 0,
        "ack_dropped": 0,
    }

    frames = fragment_message(payload, msg_id=msg_id, max_payload=max_payload)
    frames_total = len(frames)

    got_parts: dict[int, bytes] = {}
    expected_total: int | None = None
    next_expected = 0

    def send_data_frame(idx: int):
        raw = frames[idx]
        text = base64.b64encode(raw).decode("ascii")
        counters["data_sent"] += 1
        if not data_ch.send(phy_encode_text(text)):
            counters["data_dropped"] += 1

    def receiver_pump():
        nonlocal expected_total, next_expected

        while True:
            samples = data_ch.recv()
            if samples is None:
                break

            decoded_b64 = phy_decode_b64bytes(inst_rx_data, samples)
            if decoded_b64 is None:
                continue

            if corrupt_data_prob > 0.0 and random.random() < corrupt_data_prob:
                decoded_b64 = corrupt_b64_ascii(decoded_b64)

            try:
                raw_frame = base64.b64decode(decoded_b64)
                ft, mid, seq, total, part = unpack_frame(raw_frame)
                if ft != TYPE_DATA or mid != msg_id:
                    continue

                if expected_total is None:
                    expected_total = total

                got_parts[seq] = part

                # advance contiguous prefix
                while next_expected in got_parts:
                    next_expected += 1

                # IMPORTANT:
                # If we still don't have seq=0, we must NOT ACK 0 "by default".
                # Otherwise sender advances base incorrectly.
                if next_expected == 0:
                    continue

                ack_seq = next_expected - 1  # last contiguous received
                ack_frame = pack_ack(msg_id=msg_id, seq=ack_seq, total=total)
                ack_text = base64.b64encode(ack_frame).decode("ascii")

                counters["ack_sent"] += 1
                if not ack_ch.send(phy_encode_text(ack_text)):
                    counters["ack_dropped"] += 1

            except Exception:
                counters["crc_fail_total"] += 1
                continue

    def read_all_acks() -> int | None:
        best: int | None = None
        while True:
            ack_samples = ack_ch.recv()
            if ack_samples is None:
                break

            decoded = phy_decode_b64bytes(inst_rx_ack, ack_samples)
            if decoded is None:
                continue

            if corrupt_ack_prob > 0.0 and random.random() < corrupt_ack_prob:
                decoded = corrupt_b64_ascii(decoded)

            try:
                ack_frame = base64.b64decode(decoded)
                aft, amid, aseq, atotal, _ = unpack_frame(ack_frame)
                if aft == TYPE_ACK and amid == msg_id:
                    if best is None or aseq > best:
                        best = aseq
            except Exception:
                counters["crc_fail_total"] += 1
                continue

        return best

    def wait_for_progress(base: int, deadline: float) -> int:
        cur = base
        while time.time() < deadline:
            receiver_pump()
            best_ack = read_all_acks()
            if best_ack is not None and best_ack + 1 > cur:
                return min(frames_total, best_ack + 1)
            time.sleep(0.0005)
        return cur

    t0 = time.time()
    try:
        base = 0
        next_to_send = 0
        retries_at_base = 0

        while base < frames_total:
            while next_to_send < frames_total and next_to_send < base + window:
                send_data_frame(next_to_send)
                next_to_send += 1

            new_base = wait_for_progress(base, time.time() + float(timeout_s))
            if new_base > base:
                base = new_base
                retries_at_base = 0
                if next_to_send < base:
                    next_to_send = base
                continue

            counters["timeouts_total"] += 1
            retries_at_base += 1
            if retries_at_base >= max_retries:
                raise RuntimeError(f"GBN failed: too many timeouts at base={base}/{frames_total-1}")

            next_to_send = base

        receiver_pump()
        assembled = None
        if expected_total is not None:
            assembled = reassemble_frames(got_parts, expected_total)

        seconds = max(1e-9, time.time() - t0)
        ok = (assembled == payload)
        goodput = (len(payload) / seconds) if ok else 0.0

        return RunResult(
            ok=ok,
            seconds=seconds,
            goodput_Bps=goodput,
            frames_total=frames_total,
            timeouts_total=counters["timeouts_total"],
            crc_fail_total=counters["crc_fail_total"],
            data_sent=counters["data_sent"],
            data_dropped=counters["data_dropped"],
            ack_sent=counters["ack_sent"],
            ack_dropped=counters["ack_dropped"],
        )
    finally:
        free_rx(inst_rx_data)
        free_rx(inst_rx_ack)
