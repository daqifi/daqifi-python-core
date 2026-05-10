"""Tests for SerialInterface.query_binary (closes #74).

The binary-read helper backs ``NyquistDevice.refresh_metadata`` —
SYSInfoPB? returns a length-prefixed protobuf, which the existing
ASCII-decoding ``send_query`` would mangle. ``query_binary`` keeps
the response as raw bytes so the protobuf parser can decode it.
"""
from unittest.mock import MagicMock, PropertyMock

import pytest

from daqifi._internal.comm import SerialInterface


def _make_fake_serial(read_chunks):
    """Return a MagicMock simulating pyserial.Serial that yields one
    ``read_chunks`` element per ``read()`` call (when in_waiting >0)
    and reports the corresponding pending-byte count for in_waiting.
    Once exhausted, in_waiting reads as 0 and read returns b"".
    """
    FakeSerial = type("FakeSerial", (MagicMock,), {})
    s = FakeSerial()
    s.is_open = True
    s.write = MagicMock(return_value=None)
    s.flush = MagicMock()
    s.reset_input_buffer = MagicMock()

    chunks_iter = iter(read_chunks)
    queued = {"current": None}

    def in_waiting_value():
        if queued["current"] is None:
            queued["current"] = next(chunks_iter, b"")
        return len(queued["current"])

    def read_fn(_n):
        # Drain whatever's queued; pyserial's read(n) is bounded by n
        # but in_waiting matched it so just return everything.
        result = queued["current"] or b""
        queued["current"] = None
        return result

    FakeSerial.in_waiting = PropertyMock(side_effect=in_waiting_value)
    s.read = MagicMock(side_effect=read_fn)
    return s


def _make_interface_with_serial(fake_serial):
    iface = SerialInterface("/dev/ttyACM0")
    iface._serial = fake_serial
    return iface


class TestQueryBinary:
    def test_raises_when_port_not_open(self):
        iface = SerialInterface("/dev/ttyACM0")
        # _serial is None — is_open() returns False
        with pytest.raises(RuntimeError, match="not open"):
            iface.query_binary("SYSTem:SYSInfoPB?", timeout=0.05)

    def test_returns_raw_bytes_unchanged(self):
        """Binary reply must be returned as-is — no ASCII decode, no
        whitespace strip, no DAQIFI> prompt removal."""
        # Bytes that ASCII-decoded would lose information.
        payload = b"\x10\x05\x08\x01\xff\x00\x80\x7f"
        s = _make_fake_serial([payload])
        iface = _make_interface_with_serial(s)

        result = iface.query_binary("SYSTem:SYSInfoPB?", timeout=0.5, idle_settle=0.02)

        assert result == payload

    def test_writes_query_with_crlf_terminator(self):
        s = _make_fake_serial([b"\x05hello"])
        iface = _make_interface_with_serial(s)

        iface.query_binary("SYSTem:SYSInfoPB?", timeout=0.5, idle_settle=0.02)

        s.write.assert_called_once_with(b"SYSTem:SYSInfoPB?\r\n")
        s.flush.assert_called_once()

    def test_resets_input_buffer_before_writing(self):
        """Stale bytes from a prior text query must not bleed into the
        binary response — the implementation must reset_input_buffer
        before issuing the new write."""
        s = _make_fake_serial([b"\x01\x00"])
        iface = _make_interface_with_serial(s)
        call_order: list[str] = []
        s.reset_input_buffer = MagicMock(side_effect=lambda: call_order.append("reset"))
        s.write = MagicMock(side_effect=lambda _b: call_order.append("write"))

        iface.query_binary("X?", timeout=0.5, idle_settle=0.02)

        assert call_order.index("reset") < call_order.index("write")

    def test_concatenates_multiple_chunks(self):
        """A reply that arrives in two chunks (a USB CDC pattern) must
        come back as a single concatenated bytes object."""
        s = _make_fake_serial([b"\x05hello", b" world"])
        iface = _make_interface_with_serial(s)

        result = iface.query_binary("X?", timeout=1.0, idle_settle=0.02)

        assert result == b"\x05hello world"

    def test_returns_empty_bytes_on_no_response(self):
        """If the device doesn't reply within the timeout, return
        empty bytes — let the caller (e.g. refresh_metadata) decide
        how to surface the timeout."""
        s = _make_fake_serial([])  # in_waiting always 0
        iface = _make_interface_with_serial(s)

        result = iface.query_binary("X?", timeout=0.1, idle_settle=0.02)

        assert result == b""

    def test_forces_non_blocking_reads_and_restores_prior_timeout(self):
        """The serial port's pre-configured ``timeout`` would let a
        single ``read()`` block past the method's wall-clock deadline.
        ``query_binary`` must temporarily set ``timeout=0`` and
        restore the prior value when it returns."""
        s = _make_fake_serial([b"\x05hello"])
        s.timeout = 1.5  # arbitrary prior value
        iface = _make_interface_with_serial(s)
        observed_during_call: list[float | None] = []

        original_read = s.read.side_effect

        def read_with_observation(_n):
            observed_during_call.append(s.timeout)
            return original_read(_n)

        s.read = MagicMock(side_effect=read_with_observation)

        iface.query_binary("X?", timeout=0.5, idle_settle=0.02)

        # While inside query_binary, timeout was forced to 0.
        assert observed_during_call == [0]
        # And it's restored on return.
        assert s.timeout == 1.5

    def test_rejects_non_positive_timeout(self):
        s = _make_fake_serial([])
        iface = _make_interface_with_serial(s)
        with pytest.raises(ValueError, match="timeout must be > 0"):
            iface.query_binary("X?", timeout=0)
        with pytest.raises(ValueError, match="timeout must be > 0"):
            iface.query_binary("X?", timeout=-1.0)

    def test_rejects_negative_idle_settle(self):
        s = _make_fake_serial([])
        iface = _make_interface_with_serial(s)
        with pytest.raises(ValueError, match="idle_settle must be >= 0"):
            iface.query_binary("X?", timeout=1.0, idle_settle=-0.5)

    def test_idle_settle_fires_when_read_returns_empty_after_pending(self):
        """Some pyserial backends report ``in_waiting > 0`` and then
        ``read()`` returns ``b""`` (USB CDC contention). The pre-fix
        loop would never reach the idle-settle branch in that case
        and would burn the full timeout. The fixed loop must still
        exit via idle-settle: receive some bytes, then have read()
        return b"" while in_waiting is still > 0, and confirm the
        method exits well before the configured timeout."""
        FakeSerial = type("FakeSerial", (MagicMock,), {})
        s = FakeSerial()
        s.is_open = True
        s.write = MagicMock(return_value=None)
        s.flush = MagicMock()
        s.reset_input_buffer = MagicMock()
        s.timeout = 1.0

        # Sequence: first poll reports 5 pending → read returns b"hello".
        # Subsequent polls keep reporting 5 pending but read returns
        # b"" — simulating the contention bug. The loop must idle-
        # settle out via the no-bytes-this-iteration path.
        in_waiting_seq = iter([5] + [5] * 100)
        FakeSerial.in_waiting = PropertyMock(
            side_effect=lambda: next(in_waiting_seq, 0)
        )
        chunks = iter([b"hello"])
        s.read = MagicMock(side_effect=lambda _n: next(chunks, b""))

        iface = _make_interface_with_serial(s)
        import time as _time

        start = _time.monotonic()
        result = iface.query_binary("X?", timeout=2.0, idle_settle=0.05)
        elapsed = _time.monotonic() - start

        assert result == b"hello"
        # Should exit via idle-settle (~0.05s + a couple poll intervals),
        # NOT burn the full 2.0s deadline. Generous upper bound to
        # absorb scheduling jitter on slow CI.
        assert elapsed < 1.0, f"loop overran idle-settle: {elapsed:.3f}s"

    def test_restores_blocking_timeout_when_prior_was_none(self):
        """``timeout=None`` (blocking mode) is a legitimate prior
        setting — query_binary must restore None too, not just
        non-None values. Regression for Qodo PR #85
        /agentic_review pass 7 finding #1."""
        s = _make_fake_serial([b"hi"])
        s.timeout = None  # caller had the port in blocking mode
        iface = _make_interface_with_serial(s)

        iface.query_binary("X?", timeout=0.5, idle_settle=0.02)

        # Must restore to None, not leave at 0 (non-blocking).
        assert s.timeout is None

    def test_idle_settle_zero_waits_for_full_timeout(self):
        """``idle_settle=0`` disables the early-exit so the loop
        keeps polling until the deadline expires — useful when the
        caller wants to accumulate bursty traffic that may have gaps
        longer than 100ms."""
        # Bytes arrive immediately, then the source goes quiet. With
        # idle_settle=0, the loop should NOT break on the quiet
        # poll; it should wait for the full timeout.
        s = _make_fake_serial([b"early"])
        iface = _make_interface_with_serial(s)
        import time as _time

        start = _time.monotonic()
        result = iface.query_binary("X?", timeout=0.3, idle_settle=0)
        elapsed = _time.monotonic() - start

        assert result == b"early"
        # Should wait ~0.3s (with sleep granularity), NOT exit early.
        assert elapsed >= 0.25, (
            f"loop exited early despite idle_settle=0: {elapsed:.3f}s"
        )
