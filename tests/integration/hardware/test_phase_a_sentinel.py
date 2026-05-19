"""Phase A sentinel — proves the foundation fixtures work end-to-end.

These tests don't aim for coverage on any particular daqifi module.
They exercise the hardware-test scaffolding:

* ``serial_transport`` opens a raw ``SerialStreamTransport`` with no
  consumer attached, so tests own the read end of the stream
* ``clean_device`` brings the device to a known-clean state (Standby
  woken, errors drained, channels disabled)
* ``error_drainer`` returns popped errors and asserts a clean queue
  on teardown
* ``firmware_stats_diff`` snapshots ``SYST:STR:STATS?`` and exposes deltas
* ``wire_capture`` persists raw bytes to ``tests/integration/captures/``
* The auto-applied ``hardware`` marker selects these via ``pytest -m``

Once the fixtures are stable, slice files (B1..B15) build on top.
"""

from __future__ import annotations

import time

import pytest

from tests.integration.hardware._harness import FastReader


def test_clean_device_wakes_from_standby(clean_device, error_drainer):
    """After clean_device runs, no errors and the transport is connected."""
    assert clean_device.is_connected, "transport disconnected during cleanup"
    # error_drainer fixture asserts no errors at teardown — its presence
    # in the parameter list is enough to trigger the gate.
    _ = error_drainer


def test_firmware_stats_diff_snapshot(clean_device, firmware_stats_diff):
    """Open + close without streaming yields a no-drop, low-delta snapshot."""
    firmware_stats_diff.open()
    # Idle for a beat — no streaming should occur.
    time.sleep(0.5)
    firmware_stats_diff.close()
    firmware_stats_diff.assert_no_drops()
    # Sanity: stats keys we depend on exist.
    for k in ("TotalSamplesStreamed", "TotalBytesStreamed", "UsbDroppedBytes"):
        assert k in firmware_stats_diff.pre, f"missing pre-stat {k}"
        assert k in firmware_stats_diff.post, f"missing post-stat {k}"


def test_fast_reader_drains_idle_serial(clean_device):
    """FastReader can be started/stopped against an idle (non-streaming) port."""
    serial_obj = clean_device.stream
    reader = FastReader(serial_obj)
    reader.start()
    time.sleep(0.2)
    reader.stop()
    # Idle device shouldn't be sending data; bytes_received should be small.
    assert reader.bytes_received < 1024, (
        f"unexpectedly received {reader.bytes_received} bytes from idle device"
    )


def test_wire_capture_persists_bytes(clean_device, wire_capture):
    """wire_capture writes whatever bytes the test feeds it to disk."""
    wire_capture(b"hello-from-sentinel\x00\x01\x02")
    # Fixture asserts non-empty file via its print on teardown; nothing
    # else for the test to assert here.


@pytest.mark.hardware_slow
def test_short_streaming_smoke(
    clean_device, firmware_stats_diff, wire_capture, error_drainer
):
    """1-second stream at 100 Hz proves the full pipeline runs zero-drop.

    Marked ``hardware_slow`` not because 1s is slow but because anything
    that actually streams data has a higher blast radius than a static
    SCPI roundtrip — keep it out of the default ``-m hardware`` run when
    a tester wants the snappy subset.

    The ``error_drainer`` parameter assures any SCPI errors raised
    during streaming (e.g. an unrecognised channel-enable form) fail
    the test rather than being silently absorbed into the queue.
    """
    _ = error_drainer  # presence triggers the post-test queue assertion
    serial_obj = clean_device.stream
    reader = FastReader(serial_obj)
    reader.start(on_chunk=wire_capture)

    firmware_stats_diff.open()
    try:
        # Wake + enable ch0 (already done by clean_device but explicit
        # for the streaming-startup record). Each write is followed by
        # flush() so pyserial / OS buffering doesn't delay the SCPI
        # command past its accompanying sleep window.
        serial_obj.write(b"ENAble:VOLTage:DC 1\r")
        serial_obj.flush()
        time.sleep(0.2)
        serial_obj.write(b"SYSTem:STReam:START 100\r")
        serial_obj.flush()
        time.sleep(1.0)
    finally:
        # Always stop streaming on the way out — if the test body
        # raised mid-window, leaking an active stream into the
        # subsequent firmware_stats_diff.close() snapshot would
        # corrupt the SYST:STR:STATS? read with leftover protobuf
        # bytes. Best-effort because we're already in a teardown
        # path and a raise here would mask the original failure.
        try:
            serial_obj.write(b"SYSTem:STReam:STOP\r")
            serial_obj.flush()
            time.sleep(0.3)
        except Exception:
            pass
        reader.stop()
        firmware_stats_diff.close()

    # Surface anything the daemon swallowed before evaluating the
    # byte counters — a callback exception that hides bytes from
    # wire_capture would manifest as "got only X bytes" without
    # this assertion, sending the reader into a debugging rabbit
    # hole when the real problem is downstream of FastReader.
    assert reader.last_exception is None, (
        f"FastReader caught and swallowed {reader.last_exception!r}"
    )
    firmware_stats_diff.assert_no_drops()
    # Got at least *some* bytes — exact count varies with startup latency.
    assert reader.bytes_received > 100, (
        f"expected streaming bytes, got only {reader.bytes_received}"
    )
