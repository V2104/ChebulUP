from __future__ import annotations

import statistics as stats
from typing import Iterable, List, Optional

from scripts.arq_gbn import run_once, RunResult
from scripts.config import (
    DEFAULT_WINDOW,
    DEFAULT_MAX_PAYLOAD,
    DEFAULT_TIMEOUT_S,
    DEFAULT_MAX_RETRIES,
    DROP_DATA,
    DROP_ACK,
    CORRUPT_DATA,
    CORRUPT_ACK,
)


def pctl(xs: List[float], q: float) -> float:
    """
    Quantile by nearest-rank on sorted list.
    q in [0,1].
    """
    if not xs:
        return 0.0
    xs = sorted(xs)
    if q <= 0.0:
        return xs[0]
    if q >= 1.0:
        return xs[-1]
    idx = int(round(q * (len(xs) - 1)))
    return xs[idx]


def mean(xs: List[float]) -> float:
    return stats.mean(xs) if xs else 0.0


def summarize(title: str, results: List[RunResult], payload_len: int) -> None:
    print(f"\n=== {title} ===")
    runs = len(results)
    ok = [r for r in results if r.ok]

    print(f"runs: {runs}  payload: {payload_len}B")
    print(f"success: {len(ok)}/{runs} = {100.0 * len(ok) / max(1, runs):.1f}%")

    # If nothing succeeded, still print some debug-ish aggregates and exit
    if not ok:
        timeouts = [r.timeouts_total for r in results]
        crc = [r.crc_fail_total for r in results]
        print(f"timeouts: avg={mean(timeouts):.1f}  max={max(timeouts) if timeouts else 0}")
        print(f"crc_fail: avg={mean(crc):.1f}  max={max(crc) if crc else 0}")
        return

    times = [r.seconds for r in ok]
    goodputs = [r.goodput_Bps for r in ok]
    timeouts = [r.timeouts_total for r in ok]
    crc = [r.crc_fail_total for r in ok]

    print(
        "time (s): "
        f"avg={mean(times):.2f}  p50={pctl(times,0.50):.2f}  p90={pctl(times,0.90):.2f}  max={max(times):.2f}"
    )
    print(
        "goodput (B/s): "
        f"avg={mean(goodputs):.1f}  p50={pctl(goodputs,0.50):.1f}  p90={pctl(goodputs,0.90):.1f}  max={max(goodputs):.1f}"
    )
    print(
        "timeouts: "
        f"avg={mean(timeouts):.1f}  p50={pctl([float(x) for x in timeouts],0.50):.0f}  "
        f"p90={pctl([float(x) for x in timeouts],0.90):.0f}  max={max(timeouts)}"
    )
    print(
        "crc_fail: "
        f"avg={mean(crc):.1f}  p90={pctl([float(x) for x in crc],0.90):.0f}  max={max(crc)}"
    )

    # Optional: virtual-time metrics (only if your RunResult has these fields)
    if hasattr(ok[0], "virtual_seconds") and hasattr(ok[0], "goodput_virtual_Bps"):
        virt_times = [float(getattr(r, "virtual_seconds")) for r in ok]
        virt_goodputs = [float(getattr(r, "goodput_virtual_Bps")) for r in ok]

        print(
            "virtual_time (s): "
            f"avg={mean(virt_times):.2f}  p50={pctl(virt_times,0.50):.2f}  p90={pctl(virt_times,0.90):.2f}  max={max(virt_times):.2f}"
        )
        print(
            "goodput_virtual (B/s): "
            f"avg={mean(virt_goodputs):.1f}  p50={pctl(virt_goodputs,0.50):.1f}  "
            f"p90={pctl(virt_goodputs,0.90):.1f}  max={max(virt_goodputs):.1f}"
        )


def main() -> None:
    payload = b"hello world! " * 10  # 130 bytes
    seed0 = 123

    # Smoke configs: keep them short but meaningful
    configs = [
        ("GBN window=4, max_pl=32, timeout=0.2", dict(window=DEFAULT_WINDOW, max_payload=32, timeout_s=0.2)),
        ("GBN window=4, max_pl=24, timeout=0.2", dict(window=DEFAULT_WINDOW, max_payload=24, timeout_s=0.2)),
        ("GBN window=4, max_pl=32, timeout=0.4", dict(window=DEFAULT_WINDOW, max_payload=32, timeout_s=0.4)),
    ]

    runs = 20

    for title, cfg in configs:
        results: List[RunResult] = []
        for i in range(runs):
            r = run_once(
                payload=payload,
                drop_data=DROP_DATA,
                drop_ack=DROP_ACK,
                corrupt_data_prob=CORRUPT_DATA,
                corrupt_ack_prob=CORRUPT_ACK,
                max_payload=int(cfg["max_payload"]),
                timeout_s=float(cfg["timeout_s"]),
                max_retries=DEFAULT_MAX_RETRIES,
                window=int(cfg["window"]),
                seed=seed0 + i,
                msg_id=1,
            )
            results.append(r)

        summarize(title, results, payload_len=len(payload))


if __name__ == "__main__":
    main()
