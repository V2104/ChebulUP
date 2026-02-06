from __future__ import annotations

import statistics as stats

from scripts.arq_stop_and_wait import run_once, RunResult


def pctl(vals: list[float], q: float) -> float:
    s = sorted(vals)
    idx = int(round((len(s) - 1) * q))
    return s[max(0, min(len(s) - 1, idx))]


def main():
    # Grid: 4x5 = 20 configs
    max_payload_grid = [8, 16, 24, 32]
    timeout_grid = [0.2, 0.4, 0.6, 0.8, 1.0]

    runs = 10  # 10 * 20 = 200 total runs
    payload = (b"hello world! " * 10)  # 130 bytes

    drop_data = 0.25
    drop_ack = 0.10
    max_retries = 30

    print("max_pl  timeout  success  goodput_avg  time_p50  time_p90  retries_avg")
    print("------  -------  -------  -----------  --------  --------  -----------")

    for max_pl in max_payload_grid:
        for timeout_s in timeout_grid:
            results: list[RunResult] = []
            for i in range(runs):
                r = run_once(
                    payload=payload,
                    drop_data=drop_data,
                    drop_ack=drop_ack,
                    max_payload=max_pl,
                    timeout_s=timeout_s,
                    max_retries=max_retries,
                    seed=1000 + i,
                )
                results.append(r)

            ok = [r for r in results if r.ok]
            success = len(ok) / len(results)

            if not ok:
                print(f"{max_pl:6d}  {timeout_s:7.1f}  {success:7.0%}      ---        ---       ---        ---")
                continue

            times = [r.seconds for r in ok]
            gps = [r.goodput_Bps for r in ok]
            retries = [r.retries_total for r in ok]

            print(
                f"{max_pl:6d}  "
                f"{timeout_s:7.1f}  "
                f"{success:7.0%}  "
                f"{stats.mean(gps):11.1f}  "
                f"{pctl(times,0.50):8.2f}  "
                f"{pctl(times,0.90):8.2f}  "
                f"{stats.mean(retries):11.1f}"
            )


if __name__ == "__main__":
    main()
