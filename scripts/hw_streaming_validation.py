"""Comprehensive streaming validation against COM7.

Runs five tests in sequence, each through the full library stack:
  1. 10s @ 1 kHz single channel — yield should be >=95% (vs 75% in 2s window)
  2. 5s @ 5 kHz single channel
  3. 5s @ 10 kHz single channel
  4. 5s @ 1 kHz with all 16 ADC channels enabled
  5. Decode protobuf frames to verify msg_time_stamp / analog_in_data shape

Cross-checks PC frame counts against firmware SYST:STR:STATS? counters
captured before and after each rate.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from daqifi.communication import (
    CompositeMessageParser,
    StreamMessageConsumer,
)
from daqifi.transport import SerialStreamTransport
from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage


PORT = "COM7"


def send(ser, line, delay=0.2):
    ser.write((line + "\r").encode("ascii"))
    try:
        ser.flush()
    except Exception:
        pass
    time.sleep(delay)


def drain(ser, timeout=0.4) -> bytes:
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        n = ser.in_waiting
        if n > 0:
            buf.extend(ser.read(n))
        else:
            time.sleep(0.005)
    return bytes(buf)


def parse_kv_block(text: str) -> dict[str, int]:
    """Parse SYST:STR:STATS? key=value lines into a dict[str, int]."""
    out: dict[str, int] = {}
    for line in text.split("\r\n"):
        line = line.strip()
        if "=" not in line or "DAQIFI" in line or line.startswith("SYST"):
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        try:
            out[k] = int(v)
        except ValueError:
            pass
    return out


def stats_snapshot(ser) -> dict[str, int]:
    drain(ser, timeout=0.2)
    send(ser, "SYST:STR:STATS?", delay=0.5)
    raw = drain(ser, timeout=1.0)
    return parse_kv_block(raw.decode("ascii", errors="replace"))


def deep_clear(ser, max_iters=128):
    for i in range(max_iters):
        send(ser, "SYST:ERR?", delay=0.05)
        raw = drain(ser, timeout=0.2)
        if '0,"No error"' in raw.decode("ascii", errors="replace"):
            return i + 1
    return max_iters


@dataclass
class StreamRun:
    label: str
    rate_hz: int
    duration_s: float
    channels_bitmask: int
    parsed_frames: int = 0
    decoded_messages: int = 0
    decode_errors: int = 0
    sample_count_total: int = 0  # sum of len(analog_in_data) across all frames
    first_ts: int = -1
    last_ts: int = -1
    stats_pre: dict = field(default_factory=dict)
    stats_post: dict = field(default_factory=dict)


def run_stream(transport, run: StreamRun) -> None:
    ser = transport._serial
    print(f"\n=== {run.label}  rate={run.rate_hz} Hz  duration={run.duration_s}s  bitmask=0x{run.channels_bitmask:04x} ===")

    drain(ser, timeout=0.3)
    deep_clear(ser, max_iters=128)

    # Wake + enable channels
    send(ser, "SYSTem:POWer:STATe 1", delay=0.5)
    drain(ser, timeout=0.3)
    send(ser, f"ENAble:VOLTage:DC {run.channels_bitmask}", delay=0.2)
    drain(ser, timeout=0.3)

    # Pre-snapshot
    run.stats_pre = stats_snapshot(ser)
    if run.stats_pre:
        print(f"  pre  TotalSamplesStreamed={run.stats_pre.get('TotalSamplesStreamed', '?')} "
              f"TotalBytesStreamed={run.stats_pre.get('TotalBytesStreamed', '?')} "
              f"UsbDroppedBytes={run.stats_pre.get('UsbDroppedBytes', '?')} "
              f"QueueDroppedSamples={run.stats_pre.get('QueueDroppedSamples', '?')}")

    # Stream
    def on_msg(args) -> None:
        msg = getattr(args, "message", args)
        payload = getattr(msg, "data", msg)
        if not isinstance(payload, (bytes, bytearray)):
            return
        run.parsed_frames += 1
        try:
            out = DaqifiOutMessage.FromString(bytes(payload))
        except Exception:
            run.decode_errors += 1
            return
        run.decoded_messages += 1
        if out.HasField("msg_time_stamp"):
            ts = out.msg_time_stamp
            if run.first_ts < 0:
                run.first_ts = ts
            run.last_ts = ts
        if out.analog_in_data:
            run.sample_count_total += len(out.analog_in_data)

    parser = CompositeMessageParser(protobuf_factory=lambda b: bytes(b))
    consumer = StreamMessageConsumer(stream=ser, message_parser=parser)
    consumer.add_message_received(on_msg)

    consumer.start()
    try:
        send(ser, f"SYSTem:StartStreamData {run.rate_hz}", delay=0.0)
        time.sleep(run.duration_s)
    finally:
        consumer.stop()

    # Stop streaming
    send(ser, "SYSTem:StopStreamData", delay=0.5)
    drain(ser, timeout=0.5)

    # Post-snapshot
    run.stats_post = stats_snapshot(ser)
    if run.stats_post:
        print(f"  post TotalSamplesStreamed={run.stats_post.get('TotalSamplesStreamed', '?')} "
              f"TotalBytesStreamed={run.stats_post.get('TotalBytesStreamed', '?')} "
              f"UsbDroppedBytes={run.stats_post.get('UsbDroppedBytes', '?')} "
              f"QueueDroppedSamples={run.stats_post.get('QueueDroppedSamples', '?')}")

    expected = int(run.rate_hz * run.duration_s)
    pct = 100.0 * run.parsed_frames / max(expected, 1)
    print(f"  PC: parsed_frames={run.parsed_frames} (~{expected} expected, {pct:.1f}%) "
          f"decoded={run.decoded_messages} decode_errors={run.decode_errors}")
    if run.first_ts >= 0:
        ts_span = run.last_ts - run.first_ts
        print(f"  PC: first_ts={run.first_ts} last_ts={run.last_ts} span={ts_span} "
              f"sample_count_total={run.sample_count_total}")
    fw_delta_samples = (run.stats_post.get("TotalSamplesStreamed", 0)
                        - run.stats_pre.get("TotalSamplesStreamed", 0))
    fw_delta_bytes = (run.stats_post.get("TotalBytesStreamed", 0)
                      - run.stats_pre.get("TotalBytesStreamed", 0))
    fw_dropped = (run.stats_post.get("UsbDroppedBytes", 0)
                  - run.stats_pre.get("UsbDroppedBytes", 0))
    print(f"  FW: d_TotalSamplesStreamed={fw_delta_samples} d_TotalBytesStreamed={fw_delta_bytes} "
          f"d_UsbDroppedBytes={fw_dropped}")


def main() -> int:
    transport = SerialStreamTransport(
        port_name=PORT,
        baud_rate=115200,
        os_rx_buffer_bytes=65536,
        os_tx_buffer_bytes=65536,
    )
    transport.connect()
    if not transport.is_connected:
        print("connect failed")
        return 2

    try:
        runs = [
            StreamRun(label="long-window 1 kHz x 10s",
                      rate_hz=1000, duration_s=10.0, channels_bitmask=1),
            StreamRun(label="5 kHz x 5s",
                      rate_hz=5000, duration_s=5.0, channels_bitmask=1),
            StreamRun(label="10 kHz x 5s",
                      rate_hz=10000, duration_s=5.0, channels_bitmask=1),
            StreamRun(label="multi-channel all 16 @ 1 kHz x 5s",
                      rate_hz=1000, duration_s=5.0, channels_bitmask=0xFFFF),
        ]
        for r in runs:
            run_stream(transport, r)
            time.sleep(0.5)

        print("\n=== Summary ===")
        print(f"  {'label':45} {'rate':>6} {'dur':>5} {'parsed':>8} {'expected':>8} {'yield':>7} {'fwd_s':>10} {'fwd_drops':>10}")
        for r in runs:
            expected = int(r.rate_hz * r.duration_s)
            pct = 100.0 * r.parsed_frames / max(expected, 1)
            fw_d = (r.stats_post.get("TotalSamplesStreamed", 0)
                    - r.stats_pre.get("TotalSamplesStreamed", 0))
            fw_drops = (r.stats_post.get("UsbDroppedBytes", 0)
                        - r.stats_pre.get("UsbDroppedBytes", 0))
            print(f"  {r.label:45} {r.rate_hz:>6} {r.duration_s:>5.1f} {r.parsed_frames:>8} "
                  f"{expected:>8} {pct:>6.1f}% {fw_d:>10} {fw_drops:>10}")
        print(f"\n  Notes:")
        print(f"  - 'fwd_s' = post.TotalSamplesStreamed - pre.TotalSamplesStreamed (firmware counter)")
        print(f"  - 'fwd_drops' = post.UsbDroppedBytes - pre.UsbDroppedBytes (firmware counter)")
        print(f"  - 'yield' = parsed_frames / (rate*duration); <100% mostly = stream-startup latency")
        return 0
    finally:
        transport.disconnect()


if __name__ == "__main__":
    sys.exit(main())
