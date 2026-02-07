from __future__ import annotations

import statistics as stats

from scripts.arq_stop_and_wait import run_once, RunResult


def pctl(vals: list[float], q: float) -> float:
    s = sorted(vals)
    idx = int(round((len(s) - 1) * q))
    return s[max(0, min(len(s) - 1, idx))]


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
    retries = [r.retries_total for r in ok]
    crc = [r.crc_fail_total for r in ok]

    data_sent = [r.data_sent for r in ok]
    data_drop = [r.data_dropped for r in ok]
    ack_sent = [r.ack_sent for r in ok]
    ack_drop = [r.ack_dropped for r in ok]

    print(f"time (s): avg={stats.mean(times):.2f}  p50={pctl(times,0.50):.2f}  p90={pctl(times,0.90):.2f}  max={max(times):.2f}")
    print(f"goodput (B/s): avg={stats.mean(gps):.1f}  p50={pctl(gps,0.50):.1f}  p90={pctl(gps,0.90):.1f}  max={max(gps):.1f}")
    print(f"retries: avg={stats.mean(retries):.1f}  p50={pctl(retries,0.50):.0f}  p90={pctl(retries,0.90):.0f}  max={max(retries):.0f}")
    print(f"crc_fail: avg={stats.mean(crc):.1f}  p90={pctl(crc,0.90):.0f}  max={max(crc):.0f}")
    print(f"data: sent avg={stats.mean(data_sent):.1f}, dropped avg={stats.mean(data_drop):.1f}")
    print(f" ack: sent avg={stats.mean(ack_sent):.1f}, dropped avg={stats.mean(ack_drop):.1f}")


def main():
    from scripts.config import DROP_DATA, DROP_ACK, CORRUPT_DATA, CORRUPT_ACK, DEFAULT_MAX_PAYLOAD, DEFAULT_TIMEOUT_S

    # payload = 130B like your earlier runs
    payload = (b"hello world! " * 10)

    # channel loss model
    drop_data = 0.25
    drop_ack = 0.10

    # corruption model (decoded text corruption -> CRC fail)
    corrupt_data = 0.03
    corrupt_ack = 0.01

    runs = 20
    max_retries = 30

    configs = [
    (f"default {DEFAULT_MAX_PAYLOAD}/{DEFAULT_TIMEOUT_S}", DEFAULT_MAX_PAYLOAD, DEFAULT_TIMEOUT_S),
    ("alt 24/0.2", 24, 0.2),
    ("alt 32/0.4", 32, 0.4),
]


    for title, max_pl, timeout_s in configs:
        results: list[RunResult] = []
        for i in range(runs):
            r = run_once(
                payload=payload,
                drop_data=drop_data,
                drop_ack=drop_ack,
                max_payload=max_pl,
                timeout_s=timeout_s,
                max_retries=max_retries,
                seed=2000 + i,
                corrupt_data_prob=corrupt_data,
                corrupt_ack_prob=corrupt_ack,
            )
            results.append(r)

        summarize(title, results, payload_len=len(payload))


if __name__ == "__main__":
    main()
