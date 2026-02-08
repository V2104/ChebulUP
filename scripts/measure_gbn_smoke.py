from __future__ import annotations

import statistics as stats

from scripts.arq_gbn import run_once, RunResult
from scripts.config import DROP_DATA, DROP_ACK, CORRUPT_DATA, CORRUPT_ACK, DEFAULT_MAX_PAYLOAD, DEFAULT_TIMEOUT_S, DEFAULT_MAX_RETRIES


def pctl(vals: list[float], q: float) -> float:
    s = sorted(vals)
    idx = int(round((len(s) - 1) * q))
    idx = max(0, min(len(s) - 1, idx))
    return s[idx]


def summarize(title: str, results: list[RunResult], payload_len: int):
    ok = [r for r in results if r.ok]
    success = len(ok) / len(results)

    print(f"\n=== {title} ===")
    print(f"runs: {len(results)}  payload: {payload_len}B")
    print(f"success: {len(ok)}/{len(results)} = {success*100:.1f}%")

    if not ok:
        return

    times = [r.seconds for r in ok]
    gps = [r.goodput_Bps for r in ok]
    retries = [r.timeouts_total for r in ok]
    crc = [r.crc_fail_total for r in ok]

    print(f"time (s): avg={stats.mean(times):.2f}  p50={pctl(times,0.50):.2f}  p90={pctl(times,0.90):.2f}  max={max(times):.2f}")
    print(f"goodput (B/s): avg={stats.mean(gps):.1f}  p50={pctl(gps,0.50):.1f}  p90={pctl(gps,0.90):.1f}  max={max(gps):.1f}")
    print(f"timeouts: avg={stats.mean(retries):.1f}  p50={pctl(retries,0.50):.0f}  p90={pctl(retries,0.90):.0f}  max={max(retries):.0f}")
    print(f"crc_fail: avg={stats.mean(crc):.1f}  p90={pctl(crc,0.90):.0f}  max={max(crc):.0f}")


def main():
    payload = (b"hello world! " * 10)  # 130 bytes

    runs = 20
    window = 4

    configs = [
        (f"GBN window={window}, max_pl={DEFAULT_MAX_PAYLOAD}, timeout={DEFAULT_TIMEOUT_S}", DEFAULT_MAX_PAYLOAD, DEFAULT_TIMEOUT_S),
        (f"GBN window={window}, max_pl=24, timeout=0.2", 24, 0.2),
        (f"GBN window={window}, max_pl=32, timeout=0.4", 32, 0.4),
    ]

    for title, max_pl, timeout_s in configs:
        results = []
        for i in range(runs):
            r = run_once(
                payload=payload,
                drop_data=DROP_DATA,
                drop_ack=DROP_ACK,
                max_payload=max_pl,
                timeout_s=timeout_s,
                max_retries=DEFAULT_MAX_RETRIES,
                seed=4000 + i,
                window=window,
                corrupt_data_prob=CORRUPT_DATA,
                corrupt_ack_prob=CORRUPT_ACK,
            )
            results.append(r)

        summarize(title, results, payload_len=len(payload))


if __name__ == "__main__":
    main()
