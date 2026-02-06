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

SR = 48000
PROTOCOL_ID = 0


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

    # 1) ndarray
    try:
        with suppress_c_stdout_stderr():
            d = ggwave.decode(inst_rx, samples_f32)
        if d:
            return d
    except Exception:
        pass

    # 2) bytes one-shot
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
    retries_total: int
    data_sent: int
    data_dropped: int
    ack_sent: int
    ack_dropped: int


class ReceiverState:
    def __init__(self, msg_id: int):
        self.msg_id = msg_id
        self.got_parts: dict[int, bytes] = {}
        self.expected_total: int | None = None
        self.assembled: bytes | None = None

    def on_data_frame(self, raw_frame: bytes):
        ft, mid, seq, total, payload = unpack_frame(raw_frame)
        if ft != TYPE_DATA or mid != self.msg_id:
            return None

        if self.expected_total is None:
            self.expected_total = total

        self.got_parts[seq] = payload
        assembled = reassemble_frames(self.got_parts, self.expected_total)
        if assembled is not None:
            self.assembled = assembled

        return (mid, seq, total)


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
) -> RunResult:
    random.seed(seed)

    data_ch = UnreliableChannel(drop_data)
    ack_ch = UnreliableChannel(drop_ack)

    inst_rx_data = init_rx()
    inst_rx_ack = init_rx()

    counters = {
        "retries_total": 0,
        "data_sent": 0,
        "data_dropped": 0,
        "ack_sent": 0,
        "ack_dropped": 0,
    }

    rx = ReceiverState(msg_id=msg_id)

    def receiver_pump():
        while True:
            samples = data_ch.recv()
            if samples is None:
                break

            decoded_b64 = phy_decode_b64bytes(inst_rx_data, samples)
            if decoded_b64 is None:
                continue

            try:
                raw_frame = base64.b64decode(decoded_b64)
                ack_info = rx.on_data_frame(raw_frame)
                if ack_info is None:
                    continue

                mid, seq, total = ack_info
                ack_frame = pack_ack(msg_id=mid, seq=seq, total=total)
                ack_text = base64.b64encode(ack_frame).decode("ascii")

                counters["ack_sent"] += 1
                if not ack_ch.send(phy_encode_text(ack_text)):
                    counters["ack_dropped"] += 1
            except Exception:
                continue

    def sender_wait_ack(seq: int, total: int, deadline: float) -> bool:
        while time.time() < deadline:
            receiver_pump()

            ack_samples = ack_ch.recv()
            if ack_samples is None:
                time.sleep(0.001)
                continue

            decoded_b64_ack = phy_decode_b64bytes(inst_rx_ack, ack_samples)
            if decoded_b64_ack is None:
                continue

            try:
                ack_frame = base64.b64decode(decoded_b64_ack)
                aft, amid, aseq, atotal, _ = unpack_frame(ack_frame)
                if aft == TYPE_ACK and amid == msg_id and aseq == seq and atotal == total:
                    return True
            except Exception:
                continue

        return False

    t0 = time.time()
    try:
        frames = fragment_message(payload, msg_id=msg_id, max_payload=max_payload)
        frames_total = len(frames)

        for raw_frame in frames:
            ft, mid, seq, total, _ = unpack_frame(raw_frame)
            if ft != TYPE_DATA or mid != msg_id:
                raise RuntimeError("Unexpected frame in fragmentation result")

            data_text = base64.b64encode(raw_frame).decode("ascii")

            retries = 0
            while True:
                counters["data_sent"] += 1
                if not data_ch.send(phy_encode_text(data_text)):
                    counters["data_dropped"] += 1

                receiver_pump()

                got_ack = sender_wait_ack(seq, total, time.time() + float(timeout_s))
                if got_ack:
                    break

                retries += 1
                counters["retries_total"] += 1
                if retries >= max_retries:
                    raise RuntimeError(f"Too many retries on seq={seq}/{total-1}")

        receiver_pump()

        seconds = max(1e-9, time.time() - t0)
        ok = (rx.assembled == payload)
        goodput = (len(payload) / seconds) if ok else 0.0

        return RunResult(
            ok=ok,
            seconds=seconds,
            goodput_Bps=goodput,
            frames_total=frames_total,
            retries_total=counters["retries_total"],
            data_sent=counters["data_sent"],
            data_dropped=counters["data_dropped"],
            ack_sent=counters["ack_sent"],
            ack_dropped=counters["ack_dropped"],
        )
    finally:
        free_rx(inst_rx_data)
        free_rx(inst_rx_ack)
