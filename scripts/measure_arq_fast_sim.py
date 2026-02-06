from __future__ import annotations

import random
import statistics as stats
from dataclasses import dataclass
from typing import Optional

from scripts.packet import (
    TYPE_ACK,
    TYPE_DATA,
    fragment_message,
    pack_ack,
    reassemble_frames,
    unpack_frame,
)


# ----------------- Fast virtual PHY/channel -----------------

@dataclass
class ChannelParams:
    drop_prob: float = 0.0         # probability to drop a packet entirely
    corrupt_prob: float = 0.0      # probability to corrupt one byte in the frame
    delay_ms: float = 0.0          # fixed one-way delay in milliseconds


@dataclass
class VirtualPacket:
    deliver_at_ms: float
    data: bytes


class VirtualChannel:
    """
    Fast channel: stores scheduled deliveries (no real sleep).
    drop: packet is lost
    corrupt: flips one random byte (so CRC should catch it)
    delay: delivery is scheduled in simulated time
    """
    def __init__(self, params: ChannelParams):
        self.p = params
        self._q: list[VirtualPacket] = []

    def _maybe_corrupt(self, payload: bytes) -> bytes:
        if self.p.corrupt_prob <= 0:
            return payload
        if random.random() >= self.p.corrupt_prob:
            return payload
        if not payload:
            return payload
        b = bytearray(payload)
        i = random.randrange(len(b))
        b[i] ^= 0x01  # flip one bit
        return bytes(b)

    def send(self, now_ms: float, payload: bytes) -> bool:
        if self.p.drop_prob > 0 and random.random() < self.p.drop_prob:
            return False
        payload2 = self._maybe_corrupt(payload)
        deliver_at = now_ms + float(self.p.delay_ms)
        self._q.append(VirtualPacket(deliver_at_ms=deliver_at, data=payload2))
        return True

    def recv_ready(self, now_ms: float) -> list[bytes]:
        if not self._q:
            return []
        ready = [pkt for pkt in self._q if pkt.deliver_at_ms <= now_ms]
        if not ready:
            return []
        self._q = [pkt for pkt in self._q if pkt.deliver_at_ms > now_ms]
        return [pkt.data for pkt in ready]


# ----------------- ARQ simulation -----------------

@dataclass
class RunResult:
    ok: bool
    time_ms: float
    goodput_Bps: float
    frames_total: int
    retries_total: int
    data_sent: int
    data_dropped: int
    ack_sent: int
    ack_dropped: int
    crc_fail: int


def pctl(values: list[float], q: float) -> float:
    s = sorted(values)
    idx = int(round((len(s) - 1) * q))
    return s[max(0, min(len(s) - 1, idx))]


def run_once(
    *,
    payload: bytes,
    msg_id: int,
    max_payload: int,
    timeout_ms: float,
    max_retries: int,
    data_ch: VirtualChannel,
    ack_ch: VirtualChannel,
) -> RunResult:
    # Simulated time (ms)
    now_ms = 0.0
    step_ms = 1.0  # simulation tick

    counters = dict(
        retries_total=0,
        data_sent=0,
        data_dropped=0,
        ack_sent=0,
        ack_dropped=0,
        crc_fail=0,
    )

    frames = fragment_message(payload, msg_id=msg_id, max_payload=max_payload)
    frames_total = len(frames)

    # receiver state
    got_parts: dict[int, bytes] = {}
    expected_total: Optional[int] = None
    assembled: Optional[bytes] = None

    def receiver_pump():
        nonlocal expected_total, assembled, now_ms
        for pkt in data_ch.recv_ready(now_ms):
            # pkt is supposed to be a frame bytes; may be corrupted
            try:
                ft, mid, seq, total, part = unpack_frame(pkt)  # may raise on CRC
            except Exception:
                counters["crc_fail"] += 1
                continue

            if ft != TYPE_DATA or mid != msg_id:
                continue

            if expected_total is None:
                expected_total = total

            got_parts[seq] = part
            assembled2 = reassemble_frames(got_parts, expected_total)
            if assembled2 is not None:
                assembled = assembled2

            # send ACK (even for duplicates)
            ack_frame = pack_ack(msg_id=mid, seq=seq, total=total)
            counters["ack_sent"] += 1
            if not ack_ch.send(now_ms, ack_frame):
                counters["ack_dropped"] += 1

    def sender_wait_ack(seq: int, total: int, deadline_ms: float) -> bool:
        nonlocal now_ms
        while now_ms < deadline_ms:
            receiver_pump()

            for pkt in ack_ch.recv_ready(now_ms):
                try:
                    aft, amid, aseq, atotal, _ = unpack_frame(pkt)
                except Exception:
                    counters["crc_fail"] += 1
                    continue

                if aft == TYPE_ACK and amid == msg_id and aseq == seq and atotal == total:
                    return True

            now_ms += step_ms

        return False

    # main stop-and-wait
    for frame in frames:
        ft, mid, seq, total, _ = unpack_frame(frame)
        if ft != TYPE_DATA or mid != msg_id:
            return RunResult(False, now_ms, 0.0, frames_total, counters["retries_total"],
                             counters["data_sent"], counters["data_dropped"],
                             counters["ack_sent"], counters["ack_dropped"], counters["crc_fail"])

        retries = 0
        while True:
            # send DATA
            counters["data_sent"] += 1
            if not data_ch.send(now_ms, frame):
                counters["data_dropped"] += 1

            receiver_pump()

            # wait for ACK
            ok_ack = sender_wait_ack(seq, total, now_ms + timeout_ms)
            if ok_ack:
                break

            retries += 1
            counters["retries_total"] += 1
            if retries >= max_retries:
                return RunResult(False, now_ms, 0.0, frames_total, counters["retries_total"],
                                 counters["data_sent"], counters["data_dropped"],
                                 counters["ack_sent"], counters["ack_dropped"], counters["crc_fail"])

    # final pump
    receiver_pump()

    ok = (assembled == payload)
    time_ms = max(1.0, now_ms)
    goodput = (len(payload) / (time_ms / 1000.0)) if ok else 0.0

    return RunResult(
        ok=ok,
        time_ms=time_ms,
        goodput_Bps=goodput,
        frames_total=frames_total,
        retries_total=counters["retries_total"],
        data_sent=counters["data_sent"],
        data_dropped=counters["data_dropped"],
        ack_sent=counters["ack_sent"],
        ack_dropped=counters["ack_dropped"],
        crc_fail=counters["crc_fail"],
    )


def main():
    # --- knobs ---
    max_payload_grid = [8, 16, 24, 32]
    timeout_grid_ms = [50, 100, 150, 200, 300]  # FAST sim timeouts
    runs = 200  # fast-sim allows lots of runs

    payload = (b"hello world! " * 10)  # 130 bytes
    msg_id = 1
    max_retries = 50

    # Channel model: drop + delay + corrupt
    # Start with drop-only to match your previous experiment, but FAST.
    data_params = ChannelParams(drop_prob=0.25, corrupt_prob=0.03, delay_ms=20)
    ack_params  = ChannelParams(drop_prob=0.10, corrupt_prob=0.01, delay_ms=20)


    print("FAST-SIM (no ggwave DSP)")
    print(f"runs/config={runs}, payload={len(payload)}B, data={data_params}, ack={ack_params}")
    print()
    print("max_pl  timeout_ms  success  goodput_avg  time_p50  time_p90  retries_avg  crc_fail_avg")
    print("------  ---------  -------  -----------  --------  --------  -----------  -----------")

    for max_pl in max_payload_grid:
        for timeout_ms in timeout_grid_ms:
            results: list[RunResult] = []
            for i in range(runs):
                random.seed(1000 + i)  # reproducible per run
                data_ch = VirtualChannel(data_params)
                ack_ch = VirtualChannel(ack_params)

                r = run_once(
                    payload=payload,
                    msg_id=msg_id,
                    max_payload=max_pl,
                    timeout_ms=float(timeout_ms),
                    max_retries=max_retries,
                    data_ch=data_ch,
                    ack_ch=ack_ch,
                )
                results.append(r)

            ok = [r for r in results if r.ok]
            success = len(ok) / len(results)

            if not ok:
                print(f"{max_pl:6d}  {timeout_ms:9.0f}  {success:7.0%}      ---        ---       ---        ---          ---")
                continue

            times = [r.time_ms for r in ok]
            gps = [r.goodput_Bps for r in ok]
            retries = [r.retries_total for r in ok]
            crc_f = [r.crc_fail for r in ok]

            print(
                f"{max_pl:6d}  "
                f"{timeout_ms:9.0f}  "
                f"{success:7.0%}  "
                f"{stats.mean(gps):11.1f}  "
                f"{pctl(times,0.50):8.1f}  "
                f"{pctl(times,0.90):8.1f}  "
                f"{stats.mean(retries):11.1f}  "
                f"{stats.mean(crc_f):11.1f}"
            )


if __name__ == "__main__":
    main()
