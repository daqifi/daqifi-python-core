"""Streaming throughput benchmark for #18 §2.

Measures parsed-frame yield and msg_time_stamp gap rate across:

* sample rates: 100, 1000, 5000, 10000, 50000 Hz (firmware caps may
  reject some rates per channel count — those are reported as "rejected")
* channel counts: 1 (ch 0) and 16 (ch 0..15)
* consumer buffer sizes: 4 KiB (pre-PR-#19 default), 64 KiB (current
  default), 256 KiB (proposed Linux upper-bound)

For each combination, streams for ``--duration`` seconds and reports:

* ``frames_pc``     PC-side parsed-frame count
* ``frames_exp``    expected: ``rate_hz × duration_s × min(channels, 1)``
                    (one protobuf frame per trigger, regardless of channel
                    count — analog_in_data is a repeated field)
* ``yield_pct``     ``frames_pc / frames_exp × 100``
* ``ts_gap_max``    largest timestamp delta between consecutive frames
                    (1.0 = no gap)
* ``decode_err``    payloads that didn't parse as DaqifiOutMessage

Default: prints a results table to stdout. ``--out`` writes JSON for
later reduction.

Usage:
    DAQIFI_BENCHMARK_PORT=/dev/ttyACM1 python3 scripts/benchmark_streaming.py
    python3 scripts/benchmark_streaming.py --port /dev/ttyACM1 --duration 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import serial  # noqa: E402  pyserial

from daqifi._internal import scpi_commands  # noqa: E402
from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage  # noqa: E402
from daqifi.communication import CompositeMessageParser, StreamMessageConsumer  # noqa: E402


DEFAULT_RATES_HZ = (100, 1_000, 5_000, 10_000, 50_000)
DEFAULT_CHANNEL_COUNTS = (1, 16)
DEFAULT_BUFFER_SIZES = (4 * 1024, 64 * 1024, 256 * 1024)


@dataclass
class BenchmarkRun:
    rate_hz: int
    channels: int
    buffer_bytes: int
    duration_s: float
    frames_pc: int
    frames_exp: int
    yield_pct: float
    ts_gap_max: int
    decode_errors: int
    rejected: bool = False
    error: str = ""


def _send(serial_obj: serial.Serial, command: str, delay: float = 0.1) -> None:
    serial_obj.write((command + "\r").encode("ascii"))
    serial_obj.flush()
    time.sleep(delay)


def _drain(serial_obj: serial.Serial, timeout: float = 0.5) -> bytes:
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        try:
            n = serial_obj.in_waiting
        except Exception:
            break
        if n > 0:
            buf.extend(serial_obj.read(n))
        else:
            time.sleep(0.005)
    return bytes(buf)


def _channel_bitmask(num_channels: int) -> str:
    """Decimal bitmask string. ``num_channels=1`` → ``"1"`` (ch 0);
    ``num_channels=16`` → ``"65535"``. Firmware ``ENAble:VOLTage:DC``
    accepts decimal only — the typed factory tolerates hex strings, but
    the device parser does not."""
    return str((1 << num_channels) - 1)


class _StreamHarness:
    def __init__(self, serial_obj: serial.Serial, buffer_size: int) -> None:
        self.parsed_frames = 0
        self.decode_errors = 0
        self.timestamps: List[int] = []

        def _factory(payload: bytes) -> DaqifiOutMessage:
            return DaqifiOutMessage.FromString(bytes(payload))

        parser = CompositeMessageParser(protobuf_factory=_factory)
        self.consumer = StreamMessageConsumer(
            stream=serial_obj, message_parser=parser, buffer_size=buffer_size,
        )
        self.consumer.add_message_received(self._on_message)

    def _on_message(self, args) -> None:
        self.parsed_frames += 1
        msg = getattr(args, "message", None)
        out = getattr(msg, "data", None) if msg is not None else None
        if not isinstance(out, DaqifiOutMessage):
            self.decode_errors += 1
            return
        self.timestamps.append(out.msg_time_stamp)


def _max_ts_gap(timestamps: List[int]) -> int:
    if len(timestamps) < 2:
        return 0
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
    return max(gaps) if gaps else 0


def _run_one(
    serial_obj: serial.Serial,
    *,
    rate_hz: int,
    channels: int,
    buffer_bytes: int,
    duration_s: float,
) -> BenchmarkRun:
    bitmask = _channel_bitmask(channels)
    base = BenchmarkRun(
        rate_hz=rate_hz, channels=channels, buffer_bytes=buffer_bytes,
        duration_s=duration_s, frames_pc=0,
        frames_exp=int(rate_hz * duration_s),
        yield_pct=0.0, ts_gap_max=0, decode_errors=0,
    )

    try:
        _send(serial_obj, scpi_commands.disable_device_echo().data, 0.1)
        _drain(serial_obj, 0.2)
        _send(serial_obj, scpi_commands.set_protobuf_stream_format().data, 0.1)
        _drain(serial_obj, 0.2)
        _send(serial_obj, scpi_commands.enable_adc_channels(bitmask).data, 0.2)
        err = _drain(serial_obj, 0.3).decode("ascii", errors="ignore")
        if "ERROR" in err.upper():
            base.rejected = True
            base.error = err.strip().split("\n")[0][:80]
            return base

        harness = _StreamHarness(serial_obj, buffer_bytes)
        harness.consumer.start()
        try:
            # Bypass scpi_commands.start_streaming: its 1-1000 Hz cap is
            # a Python-side guard that pre-dates the firmware accepting
            # higher rates. The benchmark explicitly targets 5/10/50 kHz
            # per the #18 spec.
            _send(serial_obj, f"SYSTem:StartStreamData {rate_hz}", 0.05)
            time.sleep(duration_s)
            _send(serial_obj, scpi_commands.stop_streaming().data, 0.5)
            time.sleep(0.3)
        finally:
            harness.consumer.stop()
            harness.consumer.close()
        _drain(serial_obj, 0.5)

        base.frames_pc = harness.parsed_frames
        base.decode_errors = harness.decode_errors
        base.ts_gap_max = _max_ts_gap(harness.timestamps)
        if base.frames_exp:
            base.yield_pct = round(100.0 * base.frames_pc / base.frames_exp, 1)
        return base
    except Exception as exc:
        base.error = f"{type(exc).__name__}: {exc}"[:120]
        return base


def _emit_table(runs: List[BenchmarkRun]) -> None:
    headers = ("rate_hz", "ch", "buf_KiB", "frames_pc",
               "frames_exp", "yield_pct", "ts_gap_max", "decode_err", "note")
    rows = []
    for r in runs:
        note = "rejected" if r.rejected else (r.error or "")
        rows.append((
            str(r.rate_hz), str(r.channels), str(r.buffer_bytes // 1024),
            str(r.frames_pc), str(r.frames_exp), f"{r.yield_pct:.1f}",
            str(r.ts_gap_max), str(r.decode_errors), note[:40],
        ))
    widths = [max(len(h), max(len(r[i]) for r in rows) if rows else 0)
              for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*row))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DAQIFI_BENCHMARK_PORT", "/dev/ttyACM1"))
    ap.add_argument("--duration", type=float, default=3.0,
                    help="Stream duration per combo (seconds)")
    ap.add_argument("--rates", type=lambda s: tuple(int(x) for x in s.split(",")),
                    default=DEFAULT_RATES_HZ)
    ap.add_argument("--channels", type=lambda s: tuple(int(x) for x in s.split(",")),
                    default=DEFAULT_CHANNEL_COUNTS)
    ap.add_argument("--buffers", type=lambda s: tuple(int(x) for x in s.split(",")),
                    default=DEFAULT_BUFFER_SIZES,
                    help="Buffer sizes in bytes (comma-separated)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Optional JSON output path")
    args = ap.parse_args()

    print(f"# benchmark_streaming.py port={args.port} duration={args.duration}s")
    print(f"# rates={args.rates}  channels={args.channels}  "
          f"buffers={args.buffers}")

    runs: List[BenchmarkRun] = []
    for buffer_bytes in args.buffers:
        for channels in args.channels:
            for rate_hz in args.rates:
                serial_obj = serial.Serial(
                    port=args.port, baudrate=115200, timeout=1.0,
                )
                try:
                    print(f"  running rate={rate_hz}Hz ch={channels} "
                          f"buf={buffer_bytes // 1024}KiB ...", flush=True)
                    run = _run_one(
                        serial_obj, rate_hz=rate_hz, channels=channels,
                        buffer_bytes=buffer_bytes, duration_s=args.duration,
                    )
                    runs.append(run)
                finally:
                    serial_obj.close()
                    time.sleep(0.5)  # let the device settle between runs

    print()
    _emit_table(runs)

    if args.out is not None:
        args.out.write_text(json.dumps([asdict(r) for r in runs], indent=2))
        print(f"\nWrote JSON to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
