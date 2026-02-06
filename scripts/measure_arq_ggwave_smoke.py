from __future__ import annotations

import statistics as stats

from scripts.measure_arq import run_once, RunResult  # re-use real ggwave run_once


def pctl(vals, q):
    s = sorted(vals)
    idx = int(round((len(s) - 1) * q))
    return s[max(0, min(len(s) - 1, idx))]


def summarize(name: str, results: list[RunResult], payload_len: int):
    ok = [r for r in results if r.ok]
    success = len(ok) / len(results)

    print(f"\n=== {name} ===")
    print(f"runs: {len(results)}  payload: {payload_len}B")
    print(f"success: {len(ok)}/{len(results)} = {success:.1%}")

    if not ok:
        return

    times = [r.seconds for r in ok]
    gps = [r.goodput_Bps for r in ok]
    retries = [r.retries_total for r in ok]

    print(f"time (s): avg={stats.mean(times):.2f}  p50={pctl(times,0.50):.2f}  p90={pctl(times,0.90):.2f}  max={max(times):.2f}")
    print(f"goodput (B/s): avg={stats.mean(gps):.1f}  p50={pctl(gps,0.50):.1f}  p90={pctl(gps,0.90):.1f}  max={max(gps):.1f}")
    print(f"retries: avg={stats.mean(retries):.1f}  p50={pctl(retries,0.50):.0f}  p90={pctl(retries,0.90):.0f}  max={max(retries):.0f}")


def main():
    payload = (b"hello world! " * 10)  # 130 bytes

    # keep same loss model as before
    drop_data = 0.25
    drop_ack = 0.10
    max_retries = 30

    runs = 10  # smoke, not a full study

    configs = [
        ("32/0.2 (fast)", dict(max_payload=32, timeout_s=0.2)),
        ("32/0.4 (balanced)", dict(max_payload=32, timeout_s=0.4)),
        ("16/0.2 (fallback)", dict(max_payload=16, timeout_s=0.2)),
    ]

    for name, cfg in configs:
        results: list[RunResult] = []
        for i in range(runs):
            r = run_once(
                payload=payload,
                drop_data=drop_data,
                drop_ack=drop_ack,
                max_payload=cfg["max_payload"],
                timeout_s=cfg["timeout_s"],
                max_retries=max_retries,
                seed=2000 + i,
            )
            results.append(r)

        summarize(name, results, len(payload))


if __name__ == "__main__":
    main()
