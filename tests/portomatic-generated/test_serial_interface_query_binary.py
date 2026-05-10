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
