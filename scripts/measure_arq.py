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

# --- Protocol/PHY constants ---
SR = 48000
PROTOCOL_ID = 0


# ----------------- Logging suppression (works for C/C++) -----------------

@contextmanager
def suppress_c_stdout_stderr():
    """
    Suppress stdout/stderr at OS-level (affects C/C++ prints).
    """
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
    # Not always present, but if it is — great.
    if hasattr(ggwave, "disableLog"):
        ggwave.disableLog()
except Exception:
    pass


# ----------------- GGWave init/decode (FAST path) -----------------

def init_rx():
    params = ggwave.getDefaultParameters()
    if hasattr(params, "sampleRateInp"):
        params.sampleRateInp = float(SR)
    if hasattr(params, "sampleRateOut"):
        params.sampleRateOut = float(SR)
    return ggwave.init(params)


def decode_fast(inst_rx, samples_f32):
    """
    Fast decode for synthetic signals from ggwave.encode().
    No chunk streaming (it was the main slowdown).
    """
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


def phy_encode_text(text: str) -> bytes:
    with suppress_c_stdout_stderr():
        return ggwave.encode(text, protocolId=PROTOCOL_ID, volume=10)


def phy_decode_b64bytes(inst_rx, phy_samples: bytes) -> bytes | None:
    """
    Returns decoded base64 bytes (ASCII bytes), or None.
    """
    samples_f32 = encoded_bytes_to_f32(phy_samples)
    return decode_fast(inst_rx, samples_f32)


# ----------------- Unreliable channel (drop-only) -----------------

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


# ----------------- Metrics -----------------

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


# ----------------- ARQ (single-thread, deterministic, fast) -----------------

class ReceiverState:
    """
    Receiver runs in the same thread. We 'pump' it during sender waits.
    """
    def __init__(self, msg_id: int):
        self.msg_id = msg_id
        self.got_parts: dict[int, bytes] = {}
        self.expected_total: int | None = None
        self.assembled: bytes | None = None

    def on_data_frame(self, raw_frame: bytes):
        ft, mid, seq, total, payload = unpack_frame(raw_frame)
        if ft != TYPE_DATA or mid != self.msg_id:
            return None  # ignore
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
) -> RunResult:
    random.seed(seed)

    data_ch = UnreliableChannel(drop_data)
    ack_ch = UnreliableChannel(drop_ack)

    # Init RX instances once per run (cheap enough, but avoids threading issues)
    inst_rx_data = init_rx()
    inst_rx_ack = init_rx()

    counters = {
        "data_sent": 0,
        "data_dropped": 0,
        "ack_sent": 0,
        "ack_dropped": 0,
        "retries_total": 0,
    }

    msg_id = 1
    rx = ReceiverState(msg_id=msg_id)

    def receiver_pump():
        """
        Process all pending DATA packets and emit ACKs.
        """
        while True:
            samples = data_ch.recv()
            if samples is None:
                break

            decoded_b64 = phy_decode_b64bytes(inst_rx_data, samples)
            if decoded_b64 is None:
                continue

            try:
                raw_frame = base64.b64decode(decoded_b64)  # decoded_b64 already bytes
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
                # includes CRC fail etc.
                continue

    try:
        frames = fragment_message(payload, msg_id=msg_id, max_payload=max_payload)
        frames_total = len(frames)

        t0 = time.time()

        for raw_frame in frames:
            ft, mid, seq, total, _ = unpack_frame(raw_frame)
            if ft != TYPE_DATA or mid != msg_id:
                raise RuntimeError("unexpected frame")

            data_text = base64.b64encode(raw_frame).decode("ascii")

            retries = 0
            while True:
                # send DATA
                counters["data_sent"] += 1
                if not data_ch.send(phy_encode_text(data_text)):
                    counters["data_dropped"] += 1

                # receiver processes any data immediately
                receiver_pump()

                # wait for ACK with timeout: during waiting we keep pumping receiver too
                deadline = time.time() + timeout_s
                got_ack = False
                while time.time() < deadline:
                    # pump receiver in case data arrived but not processed yet
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
                            got_ack = True
                            break
                    except Exception:
                        continue

                if got_ack:
                    break

                retries += 1
                counters["retries_total"] += 1
                if retries >= max_retries:
                    raise RuntimeError(f"too many retries on seq={seq}/{total-1}")

        # final pump to finish assembly
        receiver_pump()

        t1 = time.time()
        seconds = max(1e-9, t1 - t0)

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

    except Exception:
        # failed run
        t1 = time.time()
        seconds = max(1e-9, t1 - t0) if "t0" in locals() else 0.0
        return RunResult(
            ok=False,
            seconds=seconds,
            goodput_Bps=0.0,
            frames_total=0,
            retries_total=counters["retries_total"],
            data_sent=counters["data_sent"],
            data_dropped=counters["data_dropped"],
            ack_sent=counters["ack_sent"],
            ack_dropped=counters["ack_dropped"],
        )

    finally:
        if hasattr(ggwave, "free"):
            try:
                ggwave.free(inst_rx_data)
            except Exception:
                pass
            try:
                ggwave.free(inst_rx_ack)
            except Exception:
                pass


# ----------------- Grid experiment -----------------

def pctl(values: list[float], q: float) -> float:
    s = sorted(values)
    idx = int(round((len(s) - 1) * q))
    return s[max(0, min(len(s) - 1, idx))]


def main():
    max_payload_grid = [8, 16, 24, 32]
    timeout_grid = [0.2, 0.4, 0.6, 0.8, 1.0]

    runs = 5  # 5*20=100 runs
    drop_data = 0.25
    drop_ack = 0.10
    max_retries = 30

    payload = (b"hello world! " * 10)  # 130 bytes

    print("max_pl  timeout  success  goodput_avg  time_p50  time_p90  retries_avg")
    print("------  -------  -------  -----------  --------  --------  -----------")

    for max_payload in max_payload_grid:
        for timeout_s in timeout_grid:
            results: list[RunResult] = []
            for i in range(runs):
                r = run_once(
                    payload=payload,
                    drop_data=drop_data,
                    drop_ack=drop_ack,
                    max_payload=max_payload,
                    timeout_s=timeout_s,
                    max_retries=max_retries,
                    seed=1000 + i,
                )
                results.append(r)

            ok_results = [r for r in results if r.ok]
            success_rate = len(ok_results) / len(results)

            if not ok_results:
                print(f"{max_payload:6d}  {timeout_s:7.1f}  {success_rate:7.0%}      ---        ---       ---        ---")
                continue

            times = [r.seconds for r in ok_results]
            gps = [r.goodput_Bps for r in ok_results]
            retries = [r.retries_total for r in ok_results]

            print(
                f"{max_payload:6d}  "
                f"{timeout_s:7.1f}  "
                f"{success_rate:7.0%}  "
                f"{sum(gps)/len(gps):11.1f}  "
                f"{pctl(times,0.50):8.2f}  "
                f"{pctl(times,0.90):8.2f}  "
                f"{sum(retries)/len(retries):11.1f}"
            )


if __name__ == "__main__":
    main()
