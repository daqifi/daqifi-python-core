"""End-to-end streaming using daqifi library + post-command error check.

Drains the error queue VERY aggressively (>10 reads) so we don't carry
stale errors into the start sequence. Then drives the device through the
canonical wake → enable → start sequence and pipes data through
CompositeMessageParser to count parsed frames.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from daqifi.communication import (
    CompositeMessageParser,
    StreamMessageConsumer,
)
from daqifi.transport import SerialStreamTransport


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


def query_err(ser) -> tuple[bool, str]:
    send(ser, "SYST:ERR?", delay=0.15)
    raw = drain(ser, timeout=0.3)
    txt = raw.decode("ascii", errors="replace")
    is_ok = '0,"No error"' in txt
    # Strip echo + prompt for compact display
    summary = txt.replace("SYST:ERR?", "").replace("DAQIFI>", "").strip()
    return is_ok, summary.split("\r\n")[0] if "\r\n" in summary else summary


def checked(ser, cmd, *, delay=0.2, label=None) -> bool:
    label = label or cmd
    send(ser, cmd, delay=delay)
    drain(ser, timeout=0.3)
    ok, errsumm = query_err(ser)
    flag = "OK " if ok else "ERR"
    print(f"  [{flag}] {label}  err={errsumm!r}")
    return ok


def deep_clear(ser, max_iters=64):
    """Drain SYST:ERR? until '0,"No error"' or max_iters."""
    for i in range(max_iters):
        ok, _ = query_err(ser)
        if ok:
            print(f"  error queue empty after {i + 1} reads")
            return
    print(f"  WARNING: error queue still non-empty after {max_iters} reads")


def test_rate(transport, log_label, rate_hz: int, duration_s: float) -> tuple[int, int]:
    """Returns (parsed_frames, raw_bytes_seen)."""
    ser = transport._serial
    print(f"\n=== {log_label}: {rate_hz} Hz for {duration_s}s ===")

    # Counters
    frame_count = 0
    raw_seen = [0]

    def on_msg(args):
        nonlocal frame_count
        msg = getattr(args, "message", args)
        payload = getattr(msg, "data", msg)
        if isinstance(payload, (bytes, bytearray)):
            frame_count += 1

    # Drain any prior data + clear errors before consumer takes over the read end
    drain(ser, timeout=0.3)
    deep_clear(ser, max_iters=64)

    # Setup: wake + enable + interface
    checked(ser, "SYSTem:POWer:STATe 1", delay=0.5,
            label="turn_device_on")
    checked(ser, "ENAble:VOLTage:DC 1", delay=0.2,
            label="enable_adc_channels(ch0)")

    # Build parser + consumer
    parser = CompositeMessageParser(protobuf_factory=lambda b: bytes(b))
    consumer = StreamMessageConsumer(stream=ser, message_parser=parser)
    consumer.add_message_received(on_msg)

    consumer.start()
    try:
        send(ser, f"SYSTem:StartStreamData {rate_hz}", delay=0.0)
        time.sleep(duration_s)
    finally:
        consumer.stop()

    # Stop
    send(ser, "SYSTem:StopStreamData", delay=0.5)
    leftover = drain(ser, timeout=0.5)
    raw_seen[0] = len(leftover)

    expected = int(rate_hz * duration_s)
    pct = 100.0 * frame_count / max(expected, 1)
    print(f"  PARSED frames={frame_count}  expected~{expected}  ({pct:.1f}%)")
    return frame_count, raw_seen[0]


def main() -> int:
    transport = SerialStreamTransport(
        port_name=PORT,
        baud_rate=115200,
        os_rx_buffer_bytes=65536,
        os_tx_buffer_bytes=65536,
    )
    transport.connect()
    if not transport.is_connected:
        print("FAILED to connect")
        return 2

    try:
        # One-time deep clear before any tests
        ser = transport._serial
        drain(ser, timeout=0.3)
        deep_clear(ser, max_iters=128)

        results = []
        for rate in (100, 1000):
            count, _ = test_rate(transport, f"rate {rate} Hz", rate, 2.0)
            results.append((rate, count))
            time.sleep(0.5)

        print("\n=== Summary ===")
        for rate, count in results:
            expected = rate * 2
            print(f"  {rate:5d} Hz: {count:6d} frames  (~{expected:6d} expected)")
        return 0
    finally:
        transport.disconnect()


if __name__ == "__main__":
    sys.exit(main())
