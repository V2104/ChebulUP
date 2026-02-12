from __future__ import annotations

import base64
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass

import ggwave

from scripts.config import PROTOCOL_ID, SR
from scripts.ggwave_codec import encoded_bytes_to_f32
from scripts.packet import (
    TYPE_ACK,
    TYPE_DATA,
    fragment_message,
    pack_ack,
    reassemble_frames,
    unpack_frame,
)

# Silence ggwave C stdout/stderr spam (optional but keeps logs clean)
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


def phy_encode_text(text: str) -> tuple[bytes, float]:
    """
    Returns:
      pcm_bytes: raw PCM int16 bytes
      dur_s: duration in seconds derived from PCM size and SR
    """
    with suppress_c_stdout_stderr():
        pcm = ggwave.encode(text, protocolId=PROTOCOL_ID, volume=10)

    # int16 PCM => 2 bytes/sample
    dur_s = (len(pcm) / 2.0) / float(SR) if pcm else 0.0
    return pcm, float(dur_s)


def phy_decode_b64bytes(inst_rx, phy_samples: bytes) -> bytes | None:
    samples_f32 = encoded_bytes_to_f32(phy_samples)

    # try ndarray
    try:
        with suppress_c_stdout_stderr():
            d = ggwave.decode(inst_rx, samples_f32)
        if d:
            return d
    except Exception:
        pass

    # try bytes
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


_B64_ALPH = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def corrupt_b64_ascii(b: bytes) -> bytes:
    if not b:
        return b
    s = bytearray(b)
    i = random.randrange(len(s))
    ch = s[i]
    if ch == ord("=") and len(s) > 1:
        i = random.randrange(len(s) - 1)
        ch = s[i]
    new = ord(_B64_ALPH[random.randrange(len(_B64_ALPH))])
    if new == ch:
        # force change
        new = ord(_B64_ALPH[( _B64_ALPH.index(chr(new)) + 1 ) % len(_B64_ALPH)])
    s[i] = new
    return bytes(s)


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

    phy_seconds: float
    virtual_seconds: float
    goodput_virtual_Bps: float


class GBNReceiver:
    """
    Classic Go-Back-N receiver:
    - accepts only in-order seq
    - sends cumulative ACK for last in-order seq (i.e., expected_seq-1)
    """
    def __init__(self, msg_id: int):
        self.msg_id = msg_id
        self.expected_seq = 0
        self.total: int | None = None
        self.parts: dict[int, bytes] = {}
        self.assembled: bytes | None = None

    def on_data(self, raw_frame: bytes) -> tuple[int, int] | None:
        ft, mid, seq, total, payload = unpack_frame(raw_frame)
        if ft != TYPE_DATA or mid != self.msg_id:
            return None

        if self.total is None:
            self.total = total

        # accept only in-order
        if seq != self.expected_seq:
            # still ACK last in-order
            ack_seq = self.expected_seq - 1
            return (mid, max(-1, ack_seq))

        self.parts[seq] = payload
        self.expected_seq += 1

        if self.total is not None:
            assembled = reassemble_frames(self.parts, self.total)
            if assembled is not None:
                self.assembled = assembled

        # ACK just received seq (last in-order)
        return (mid, seq)


def run_once(
    *,
    payload: bytes,
    drop_data: float,
    drop_ack: float,
    max_payload: int,
    timeout_s: float,
    max_timeouts: int | None = None,
    max_retries: int | None = None,  # backward compatible alias
    seed: int = 1,
    msg_id: int = 1,
    window: int = 4,
    corrupt_data_prob: float = 0.0,
    corrupt_ack_prob: float = 0.0,
) -> RunResult:
    """
    GBN sender+receiver simulation over ggwave-based PHY.
    Time model:
      phy_seconds = sum(duration of each transmitted PCM (DATA+ACK), even if dropped)
      virtual_seconds = phy_seconds + timeouts_total * timeout_s
    """
    random.seed(seed)

    # Backward compatibility: old callers use max_retries
    if max_timeouts is None and max_retries is None:
        max_timeouts = 20
    elif max_timeouts is None and max_retries is not None:
        max_timeouts = int(max_retries)
    elif max_timeouts is not None and max_retries is None:
        max_timeouts = int(max_timeouts)
    else:
        # both provided: prefer max_timeouts explicitly
        max_timeouts = int(max_timeouts)


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

    rx = GBNReceiver(msg_id=msg_id)

    # Pre-fragment
    frames = fragment_message(payload, msg_id=msg_id, max_payload=max_payload)
    frames_total = len(frames)

    # Sender state
    base = 0
    next_to_send = 0
    last_ack = -1

    phy_seconds = 0.0  # sum of “on-air” durations inferred from PCM length

    def receiver_pump():
        """Process all pending DATA frames, and emit ACKs."""
        nonlocal phy_seconds
        while True:
            samples = data_ch.recv()
            if samples is None:
                break

            decoded_b64 = phy_decode_b64bytes(inst_rx_data, samples)
            if decoded_b64 is not None and random.random() < corrupt_data_prob:
                decoded_b64 = corrupt_b64_ascii(decoded_b64)

            if decoded_b64 is None:
                continue

            try:
                raw = base64.b64decode(decoded_b64)
                ack_info = rx.on_data(raw)
                if ack_info is None:
                    continue

                amid, ack_seq = ack_info

                # Encode ACK frame (cumulative)
                # total must be the same as in data; we keep total from receiver
                total = rx.total if rx.total is not None else frames_total
                ack_frame = pack_ack(msg_id=amid, seq=ack_seq, total=total)
                ack_text = base64.b64encode(ack_frame).decode("ascii")

                pcm, dur_s = phy_encode_text(ack_text)

                counters["ack_sent"] += 1
                if not ack_ch.send(pcm):
                    counters["ack_dropped"] += 1

            except Exception:
                counters["crc_fail_total"] += 1
                continue

    def sender_pump_ack(deadline: float) -> int | None:
        """
        Return last cumulative ACK seq (>=0) if received before deadline.
        """
        nonlocal phy_seconds

        while time.time() < deadline:
            receiver_pump()

            ack_samples = ack_ch.recv()
            if ack_samples is None:
                time.sleep(0.001)
                continue

            decoded_b64 = phy_decode_b64bytes(inst_rx_ack, ack_samples)
            if decoded_b64 is not None and random.random() < corrupt_ack_prob:
                decoded_b64 = corrupt_b64_ascii(decoded_b64)

            if decoded_b64 is None:
                continue

            try:
                ack_raw = base64.b64decode(decoded_b64)
                ft, mid, seq, total, _ = unpack_frame(ack_raw)
                if ft == TYPE_ACK and mid == msg_id:
                    return seq
            except Exception:
                counters["crc_fail_total"] += 1
                continue

        return None

    def send_data_frame(i: int):
        nonlocal phy_seconds
        raw = frames[i]
        text = base64.b64encode(raw).decode("ascii")

        pcm, dur_s = phy_encode_text(text)
        phy_seconds += dur_s

        counters["data_sent"] += 1
        if not data_ch.send(pcm):
            counters["data_dropped"] += 1

    t0 = time.time()
    try:
        if window < 1:
            raise ValueError("window must be >= 1")

        # Main loop
        while base < frames_total:
            # send new frames up to window
            while next_to_send < frames_total and next_to_send < base + window:
                send_data_frame(next_to_send)
                next_to_send += 1

            receiver_pump()

            ack_seq = sender_pump_ack(time.time() + float(timeout_s))
            if ack_seq is None:
                counters["timeouts_total"] += 1
                if counters["timeouts_total"] >= max_timeouts:
                    raise RuntimeError(f"GBN failed: too many timeouts at base={base}/{frames_total-1}")

                # Timeout => retransmit from base (Go-Back-N)
                next_to_send = base
                continue

            # cumulative ACK received:
            # move base to ack_seq+1 if it advances
            if ack_seq > last_ack:
                last_ack = ack_seq
                base = max(base, ack_seq + 1)

        receiver_pump()

        seconds = max(1e-9, time.time() - t0)
        ok = (rx.assembled == payload)
        goodput = (len(payload) / seconds) if ok else 0.0

        # Virtual model: on-air + waiting due to timeouts
        virtual_seconds = phy_seconds + float(counters["timeouts_total"]) * float(timeout_s)
        virtual_seconds = max(1e-9, virtual_seconds)
        goodput_virtual = (len(payload) / virtual_seconds) if ok else 0.0

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
            phy_seconds=phy_seconds,
            virtual_seconds=virtual_seconds,
            goodput_virtual_Bps=goodput_virtual,
        )
    finally:
        free_rx(inst_rx_data)
        free_rx(inst_rx_ack)
