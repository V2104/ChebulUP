from __future__ import annotations

import random
import statistics as stats
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict

from scripts.packet import (
    TYPE_ACK,
    TYPE_DATA,
    fragment_message,
    pack_ack,
    reassemble_frames,
    unpack_frame,
)


# -----------------------------
# Models / results
# -----------------------------

@dataclass(frozen=True)
class ChannelParams:
    drop_prob: float
    corrupt_prob: float
    delay_ms: int


@dataclass
class RunResult:
    ok: bool
    time_ms: int
    goodput_Bps: float
    retries_total: int
    crc_fail_total: int


# -----------------------------
# Helpers
# -----------------------------

def _pctl(vals: List[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = int(round((len(s) - 1) * q))
    idx = max(0, min(len(s) - 1, idx))
    return float(s[idx])


def _corrupt_bytes(b: bytes, rng: random.Random) -> bytes:
    """Flip one random bit in the payload to trigger CRC failure downstream."""
    if not b:
        return b
    arr = bytearray(b)
    i = rng.randrange(len(arr))
    bit = 1 << rng.randrange(8)
    arr[i] ^= bit
    return bytes(arr)


# -----------------------------
# Event-driven channel simulator
# -----------------------------

class EventQueue:
    """Simple time-ordered queue of (deliver_time_ms, bytes)."""

    def __init__(self) -> None:
        self._items: List[Tuple[int, bytes]] = []

    def push(self, t_ms: int, payload: bytes) -> None:
        self._items.append((t_ms, payload))

    def pop_ready(self, now_ms: int) -> List[bytes]:
        if not self._items:
            return []
        ready: List[bytes] = []
        rest: List[Tuple[int, bytes]] = []
        for t, p in self._items:
            if t <= now_ms:
                ready.append(p)
            else:
                rest.append((t, p))
        self._items = rest
        return ready

    def next_time(self) -> Optional[int]:
        if not self._items:
            return None
        return min(t for t, _ in self._items)


def run_once_sim(
    *,
    payload: bytes,
    max_payload: int,
    timeout_ms: int,
    max_retries: int,
    data_ch: ChannelParams,
    ack_ch: ChannelParams,
    seed: int,
    msg_id: int = 1,
) -> RunResult:
    """
    Stop-and-wait ARQ simulation WITHOUT ggwave:
      - fragmentation/CRC are real (via scripts.packet)
      - channel effects are simulated (drop/corrupt/delay)
      - time advances event-by-event (fast)
    """
    rng = random.Random(seed)

    # event queues for "in-flight" packets
    q_data = EventQueue()
    q_ack = EventQueue()

    now_ms = 0
    retries_total = 0
    crc_fail_total = 0

    # receiver assembly state
    got_parts: Dict[int, bytes] = {}
    expected_total: Optional[int] = None

    frames = fragment_message(payload, msg_id=msg_id, max_payload=max_payload)
    frames_total = len(frames)

    def send_over_channel(q: EventQueue, raw: bytes, ch: ChannelParams) -> None:
        nonlocal now_ms
        if rng.random() < ch.drop_prob:
            return
        if ch.corrupt_prob > 0.0 and rng.random() < ch.corrupt_prob:
            raw = _corrupt_bytes(raw, rng)
        q.push(now_ms + int(ch.delay_ms), raw)

    def receiver_pump() -> None:
        nonlocal expected_total, crc_fail_total

        # process all arrived DATA frames
        for raw_frame in q_data.pop_ready(now_ms):
            try:
                ft, mid, seq, total, part = unpack_frame(raw_frame)  # CRC checked inside
                if ft != TYPE_DATA or mid != msg_id:
                    continue

                if expected_total is None:
                    expected_total = total

                got_parts[seq] = part

                # send ACK for this seq
                ack_frame = pack_ack(msg_id=mid, seq=seq, total=total)
                send_over_channel(q_ack, ack_frame, ack_ch)

            except Exception:
                # CRC/base parse failure
                crc_fail_total += 1
                continue

    def sender_wait_ack(seq: int, total: int, deadline_ms: int) -> bool:
        nonlocal now_ms, crc_fail_total

        while now_ms <= deadline_ms:
            receiver_pump()

            # check all arrived ACK frames
            for raw_ack in q_ack.pop_ready(now_ms):
                try:
                    aft, amid, aseq, atotal, _ = unpack_frame(raw_ack)
                    if aft == TYPE_ACK and amid == msg_id and aseq == seq and atotal == total:
                        return True
                except Exception:
                    crc_fail_total += 1
                    continue

            # advance time to next event or to deadline
            next_t = None
            t1 = q_data.next_time()
            t2 = q_ack.next_time()
            if t1 is not None and t2 is not None:
                next_t = min(t1, t2)
            else:
                next_t = t1 if t2 is None else t2

            if next_t is None:
                # nothing in-flight: jump straight to deadline
                now_ms = deadline_ms + 1
            else:
                if next_t <= now_ms:
                    # should not happen often, but avoid infinite loops
                    now_ms += 1
                else:
                    now_ms = min(next_t, deadline_ms + 1)

        return False

    # main sender loop
    for raw_frame in frames:
        ft, mid, seq, total, _ = unpack_frame(raw_frame)
        if ft != TYPE_DATA or mid != msg_id:
            return RunResult(ok=False, time_ms=now_ms, goodput_Bps=0.0, retries_total=retries_total, crc_fail_total=crc_fail_total)

        tries = 0
        while True:
            send_over_channel(q_data, raw_frame, data_ch)

            got_ack = sender_wait_ack(seq, total, now_ms + timeout_ms)
            if got_ack:
                break

            tries += 1
            retries_total += 1
            if tries >= max_retries:
                return RunResult(ok=False, time_ms=now_ms, goodput_Bps=0.0, retries_total=retries_total, crc_fail_total=crc_fail_total)

    # final receiver pump to assemble
    receiver_pump()
    assembled = None
    if expected_total is not None:
        assembled = reassemble_frames(got_parts, expected_total)

    ok = (assembled == payload)

    time_ms = max(1, now_ms)  # avoid division by zero
    seconds = time_ms / 1000.0
    goodput = (len(payload) / seconds) if ok else 0.0

    return RunResult(
        ok=ok,
        time_ms=time_ms,
        goodput_Bps=goodput,
        retries_total=retries_total,
        crc_fail_total=crc_fail_total,
    )


# -----------------------------
# Benchmark grid + TOP-3
# -----------------------------

def main() -> int:
    payload = (b"hello world! " * 10)  # 130 bytes

    from scripts.config import DROP_DATA, DROP_ACK, CORRUPT_DATA, CORRUPT_ACK, DEFAULT_MAX_PAYLOAD, DEFAULT_TIMEOUT_MS
    data = ChannelParams(drop_prob=DROP_DATA, corrupt_prob=CORRUPT_DATA, delay_ms=20)
    ack = ChannelParams(drop_prob=DROP_ACK, corrupt_prob=CORRUPT_ACK, delay_ms=20)

    runs_per_config = 200
    max_retries = 50

    max_payload_grid = [8, 16, 24, 32]
    timeout_grid_ms = [50, 100, 150, 200, 300]

    print("\nFAST-SIM (no ggwave DSP)")
    print(
        f"runs/config={runs_per_config}, payload={len(payload)}B, "
        f"data={data}, ack={ack}\n"
    )

    header = "max_pl  timeout_ms  success  goodput_avg  time_p50  time_p90  retries_avg  crc_fail_avg"
    print(header)
    print("------  ---------  -------  -----------  --------  --------  -----------  -----------")

    rows: List[dict] = []

    for max_pl in max_payload_grid:
        for timeout_ms in timeout_grid_ms:
            results: List[RunResult] = []
            for i in range(runs_per_config):
                r = run_once_sim(
                    payload=payload,
                    max_payload=max_pl,
                    timeout_ms=timeout_ms,
                    max_retries=max_retries,
                    data_ch=data,
                    ack_ch=ack,
                    seed=100000 + (max_pl * 1000) + (timeout_ms * 10) + i,
                )
                results.append(r)

            ok = [r for r in results if r.ok]
            success = (len(ok) / len(results)) if results else 0.0

            # For metrics, consider only successful runs (honest "goodput")
            if ok:
                goodputs = [r.goodput_Bps for r in ok]
                times = [r.time_ms for r in ok]
                retries = [r.retries_total for r in ok]
                crc = [r.crc_fail_total for r in ok]

                goodput_avg = stats.mean(goodputs)
                time_p50 = _pctl(times, 0.50)
                time_p90 = _pctl(times, 0.90)
                retries_avg = stats.mean(retries)
                crc_fail_avg = stats.mean(crc)
            else:
                goodput_avg = 0.0
                time_p50 = 0.0
                time_p90 = 0.0
                retries_avg = 0.0
                crc_fail_avg = 0.0

            print(
                f"{max_pl:6d}  {timeout_ms:9d}  {success*100:6.0f}%  "
                f"{goodput_avg:11.1f}  {time_p50:8.1f}  {time_p90:8.1f}  "
                f"{retries_avg:11.1f}  {crc_fail_avg:11.1f}"
            )

            rows.append({
                "max_pl": max_pl,
                "timeout_ms": timeout_ms,
                "success": success,
                "goodput_avg": goodput_avg,
                "time_p50": time_p50,
                "time_p90": time_p90,
                "retries_avg": retries_avg,
                "crc_fail_avg": crc_fail_avg,
            })

    # TOP-3 selection among configs with 100% success
    cands = [r for r in rows if r["success"] >= 1.0]
    cands.sort(key=lambda r: (r["goodput_avg"], -r["time_p50"]), reverse=True)

    print("\nTOP-3 (success=100%):")
    for r in cands[:3]:
        print(
            f"  max_pl={r['max_pl']:2d}  timeout_ms={r['timeout_ms']:3d}  "
            f"goodput_avg={r['goodput_avg']:.1f}  time_p50={r['time_p50']:.1f}  "
            f"retries_avg={r['retries_avg']:.1f}  crc_fail_avg={r['crc_fail_avg']:.1f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
