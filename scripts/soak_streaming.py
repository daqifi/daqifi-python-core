"""Long-running streaming soak test.

Streams 1ch @ 100Hz indefinitely (or for ``--duration-hours`` hours)
and logs StreamConsumerTelemetry every ``--report-seconds`` seconds.
Surfaces:

* sustained yield (frames received vs expected, rolling window)
* stream gaps (any time the consumer sees zero msgs in a report window)
* read syscall rate / payload distribution
* parse cost ratio

Use as the unattended overnight stability check called out in #18 §3:

    python3 scripts/soak_streaming.py --port /dev/ttyACM0 --duration-hours 6

Press Ctrl-C to stop early. Output is written to stdout AND, when
``--out`` is given, appended as one JSON line per report interval.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional, TextIO

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import serial  # noqa: E402

from daqifi._internal import scpi_commands  # noqa: E402
from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage  # noqa: E402
from daqifi.communication import CompositeMessageParser, StreamMessageConsumer  # noqa: E402


_stop_requested = False


def _request_stop(_signo, _frame):
    global _stop_requested
    _stop_requested = True
    print("\n# stop requested — finishing report cycle", flush=True)


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


class _SoakHarness:
    """Tracks message counts across the soak. ``parsed_frames`` is a
    plain int — the consumer thread is the only writer."""

    def __init__(self):
        self.parsed_frames = 0
        self.decode_errors = 0

    def __call__(self, args) -> None:
        self.parsed_frames += 1
        msg = getattr(args, "message", None)
        out = getattr(msg, "data", None) if msg is not None else None
        if not isinstance(out, DaqifiOutMessage):
            self.decode_errors += 1


def _emit_report(
    iteration: int,
    elapsed_s: float,
    interval_s: float,
    rate_hz: int,
    harness: _SoakHarness,
    consumer: StreamMessageConsumer,
    last_total: int,
    out_fh: Optional[TextIO],
) -> int:
    """Print one report line and return the new ``last_total``."""
    total = harness.parsed_frames
    delta = total - last_total
    expected = int(rate_hz * interval_s)
    yield_pct = round(100.0 * delta / expected, 1) if expected else 0.0
    overall_expected = int(rate_hz * elapsed_s)
    overall_yield = (
        round(100.0 * total / overall_expected, 1) if overall_expected else 0.0
    )
    tel = consumer.telemetry
    payload = {
        "iter": iteration,
        "elapsed_s": round(elapsed_s, 1),
        "frames_total": total,
        "frames_window": delta,
        "yield_window_pct": yield_pct,
        "yield_overall_pct": overall_yield,
        "decode_errors": harness.decode_errors,
        "read_calls": tel.read_calls,
        "read_bytes_avg": round(tel.read_bytes_avg, 1),
        "read_bytes_max": tel.read_bytes_max,
        "read_seconds": round(tel.read_seconds, 3),
        "parse_calls": tel.parse_calls,
        "parse_seconds": round(tel.parse_seconds, 3),
        "messages_emitted": tel.messages_emitted,
    }
    print(
        f"[{iteration:>5}] elapsed={elapsed_s/60:>6.2f}m "
        f"win={delta:>6} ({yield_pct:>5.1f}%) "
        f"overall={total:>9} ({overall_yield:>5.1f}%) "
        f"reads={tel.read_calls:>7} avg={tel.read_bytes_avg:>6.0f}B "
        f"parse_ratio={(tel.parse_seconds / tel.read_seconds * 100 if tel.read_seconds else 0):>4.1f}%",
        flush=True,
    )
    if out_fh is not None:
        out_fh.write(json.dumps(payload) + "\n")
        out_fh.flush()
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DAQIFI_BENCHMARK_PORT", "/dev/ttyACM0"))
    ap.add_argument("--rate-hz", type=int, default=100)
    ap.add_argument("--channels", type=int, default=1, help="Number of channels (1=ch0, 16=ch0..15)")
    ap.add_argument("--duration-hours", type=float, default=6.0,
                    help="Soak duration; 0 = run until Ctrl-C")
    ap.add_argument("--report-seconds", type=float, default=60.0)
    ap.add_argument("--out", type=Path, default=None,
                    help="Optional JSONL output path (one line per report)")
    args = ap.parse_args()

    bitmask = str((1 << args.channels) - 1)
    duration_s = args.duration_hours * 3600.0
    print(f"# soak_streaming.py port={args.port} rate={args.rate_hz}Hz "
          f"channels={args.channels} duration={'unbounded' if duration_s == 0 else f'{args.duration_hours:.2f}h'}")
    print(f"# reporting every {args.report_seconds:.0f}s; SIGINT to stop early")

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    out_fh = args.out.open("a") if args.out else None
    serial_obj = serial.Serial(port=args.port, baudrate=115200, timeout=1.0)
    try:
        _send(serial_obj, scpi_commands.disable_device_echo().data, 0.1)
        _drain(serial_obj, 0.2)
        _send(serial_obj, scpi_commands.set_protobuf_stream_format().data, 0.1)
        _drain(serial_obj, 0.2)
        _send(serial_obj, scpi_commands.enable_adc_channels(bitmask).data, 0.2)
        _drain(serial_obj, 0.3)

        harness = _SoakHarness()
        parser = CompositeMessageParser(
            protobuf_factory=lambda b: DaqifiOutMessage.FromString(bytes(b))
        )
        consumer = StreamMessageConsumer(stream=serial_obj, message_parser=parser)
        consumer.add_message_received(harness)
        consumer.start()
        try:
            _send(serial_obj, scpi_commands.start_streaming(args.rate_hz).data, 0.1)
            start = time.monotonic()
            iteration = 0
            last_total = 0
            next_report = start + args.report_seconds
            deadline = start + duration_s if duration_s > 0 else float("inf")

            while time.monotonic() < deadline and not _stop_requested:
                time.sleep(0.5)
                if time.monotonic() >= next_report:
                    iteration += 1
                    elapsed = time.monotonic() - start
                    last_total = _emit_report(
                        iteration, elapsed, args.report_seconds, args.rate_hz,
                        harness, consumer, last_total, out_fh,
                    )
                    next_report += args.report_seconds

            # Final report covering the partial last window.
            elapsed = time.monotonic() - start
            iteration += 1
            _emit_report(
                iteration, elapsed,
                elapsed - (iteration - 1) * args.report_seconds,
                args.rate_hz, harness, consumer, last_total, out_fh,
            )

            _send(serial_obj, scpi_commands.stop_streaming().data, 0.5)
            time.sleep(0.5)
        finally:
            consumer.stop()
            consumer.close()
        _drain(serial_obj, 0.5)
    finally:
        serial_obj.close()
        if out_fh is not None:
            out_fh.close()

    print(f"\n# soak complete: {harness.parsed_frames} frames, "
          f"{harness.decode_errors} decode errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
