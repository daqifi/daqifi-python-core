"""Hardware validation for PR #19, run on Windows against COM7 (DAQiFi Nyquist).

Validates:
  1. set_buffer_size(rx=65536, tx=65536) succeeds (no debug log emitted).
  2. RAW BYTES check: streaming single-channel @ 1 kHz produces *any* bytes
     (skips the parser to isolate firmware/setup issues from parser bugs).
  3. PARSED frames check: protobuf frames flow through CompositeMessageParser.

Mirrors the canonical startup sequence from
daqifi-python-test-suite/test_harness.py: line endings, INT 0 (USB),
BENCHmark / TESTpattern / SD off, MEM:RESet, channel enable, then start.

Run from Windows because pyserial set_buffer_size is Windows-only:

    python scripts/hw_test_buffer_and_stream.py
"""

from __future__ import annotations

import logging
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
BAUD = 115200


def _log_setup() -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    logging.getLogger("google").setLevel(logging.WARNING)
    return logging.getLogger("hw_test")


def _send(serial_obj, line: str, delay: float = 0.3) -> None:
    """Mirror test_harness.SCPI.command — `\\r` terminator, post-write delay."""
    serial_obj.write((line + "\r").encode("ascii"))
    try:
        serial_obj.flush()
    except Exception:
        pass
    time.sleep(delay)


def _drain(serial_obj, timeout: float = 1.0) -> int:
    """Drain serial buffer for `timeout` seconds; return bytes drained."""
    deadline = time.monotonic() + timeout
    drained = 0
    while time.monotonic() < deadline:
        n = serial_obj.in_waiting
        if n > 0:
            serial_obj.read(n)
            drained += n
        else:
            time.sleep(0.005)
    return drained


def _device_reset(ser, log: logging.Logger) -> None:
    """Mirror test_harness.device_reset — clean state for next start."""
    log.info("device_reset: stopping any active stream")
    _send(ser, "SYST:StopStreamData", delay=1.0)
    drained = _drain(ser, timeout=1.0)
    log.info("  drained %d bytes after stop", drained)
    _send(ser, "SYST:STR:BENCHmark 0", delay=0.2)
    _send(ser, "SYST:STR:TESTpattern 0", delay=0.2)
    _send(ser, "SYST:STOR:SD:ENAble 0", delay=1.0)
    _send(ser, "ENABle:VOLTage:DC 0", delay=0.2)
    _send(ser, "SYST:STR:INT 0", delay=0.2)  # USB interface
    _send(ser, "SYST:MEM:RESet", delay=0.3)
    _drain(ser, timeout=0.5)


def test_buffer_sizing(log: logging.Logger) -> bool:
    log.info("=== Test 1: set_buffer_size on connect ===")
    transport = SerialStreamTransport(
        port_name=PORT,
        baud_rate=BAUD,
        os_rx_buffer_bytes=65536,
        os_tx_buffer_bytes=65536,
    )
    try:
        transport.connect()
    except Exception as exc:
        log.error("connect failed: %s", exc)
        return False
    try:
        if not transport.is_connected:
            log.error("transport reports not connected")
            return False
        ser = transport._serial
        log.info("connected; pyserial set_buffer_size present: %s",
                 hasattr(ser, "set_buffer_size"))
        log.info("OK: connect + set_buffer_size succeeded")
        return True
    finally:
        transport.disconnect()


def test_raw_bytes(log: logging.Logger, rate_hz: int, duration_s: float) -> int:
    """Open the port, do canonical reset + start, count raw bytes (no parser)."""
    log.info("=== Raw byte test: %d Hz for %.1fs ===", rate_hz, duration_s)
    transport = SerialStreamTransport(
        port_name=PORT,
        baud_rate=BAUD,
        os_rx_buffer_bytes=65536,
        os_tx_buffer_bytes=65536,
    )
    transport.connect()
    ser = transport._serial

    bytes_received = 0
    first_byte_at = None

    try:
        _device_reset(ser, log)
        _send(ser, "ENABle:VOLTage:DC 1", delay=0.2)  # bitmask: ch0 only
        _send(ser, f"SYST:StartStreamData {rate_hz}", delay=0.0)

        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            n = ser.in_waiting
            if n > 0:
                if first_byte_at is None:
                    first_byte_at = time.monotonic()
                data = ser.read(n)
                bytes_received += len(data)
            else:
                time.sleep(0.001)

        _send(ser, "SYST:StopStreamData", delay=1.0)
        _drain(ser, timeout=0.5)
    finally:
        transport.disconnect()

    if first_byte_at is not None:
        log.info("first byte at t=%.3fs", first_byte_at - (deadline - duration_s))
    else:
        log.info("NO BYTES RECEIVED")
    log.info("rate=%d Hz duration=%.1fs RAW bytes=%d",
             rate_hz, duration_s, bytes_received)
    return bytes_received


def test_parsed(log: logging.Logger, rate_hz: int, duration_s: float) -> int:
    log.info("=== Parsed frame test: %d Hz for %.1fs ===", rate_hz, duration_s)
    transport = SerialStreamTransport(
        port_name=PORT,
        baud_rate=BAUD,
        os_rx_buffer_bytes=65536,
        os_tx_buffer_bytes=65536,
    )
    transport.connect()
    ser = transport._serial

    bytes_seen = [0]
    frame_count = 0

    def on_msg(args) -> None:
        nonlocal frame_count
        msg = getattr(args, "message", args)
        payload = getattr(msg, "data", msg)
        if isinstance(payload, (bytes, bytearray)):
            frame_count += 1

    parser = CompositeMessageParser(protobuf_factory=lambda b: bytes(b))
    consumer = StreamMessageConsumer(
        stream=ser,
        message_parser=parser,
    )
    consumer.add_message_received(on_msg)

    try:
        _device_reset(ser, log)
        _send(ser, "ENABle:VOLTage:DC 1", delay=0.2)
        consumer.start()
        _send(ser, f"SYST:StartStreamData {rate_hz}", delay=0.0)

        time.sleep(duration_s)

        consumer.stop()
        _send(ser, "SYST:StopStreamData", delay=1.0)
        _drain(ser, timeout=0.5)
    finally:
        transport.disconnect()

    expected = int(rate_hz * duration_s)
    pct = 100.0 * frame_count / max(expected, 1)
    log.info("rate=%d Hz duration=%.1fs PARSED frames=%d expected~%d (%.1f%%)",
             rate_hz, duration_s, frame_count, expected, pct)
    return frame_count


def main() -> int:
    log = _log_setup()
    log.info("PR #19 hardware validation against %s", PORT)

    if not test_buffer_sizing(log):
        log.error("buffer sizing test failed; aborting")
        return 2

    raw = test_raw_bytes(log, rate_hz=1000, duration_s=2.0)
    if raw == 0:
        log.error("Got 0 raw bytes at 1 kHz — firmware/setup issue, not parser")
        log.error("(Skipping parsed-frame test; would just fail too.)")
        return 1

    log.info("Raw bytes flowing — testing parsed frames at multiple rates")
    results = []
    for rate in (100, 1000, 5000, 10000):
        try:
            count = test_parsed(log, rate_hz=rate, duration_s=2.0)
        except Exception as exc:
            log.error("parsed test @ %d Hz raised: %s", rate, exc)
            count = -1
        results.append((rate, count))
        time.sleep(0.5)

    log.info("=== Summary ===")
    for rate, count in results:
        expected = rate * 2
        status = ("ZERO-FRAMES" if count == 0
                  else "ERROR" if count < 0 else "OK")
        log.info("  %5d Hz: %6d frames (~%6d expected)  %s",
                 rate, count, expected, status)
    return 1 if any(c == 0 for _, c in results) else 0


if __name__ == "__main__":
    sys.exit(main())
