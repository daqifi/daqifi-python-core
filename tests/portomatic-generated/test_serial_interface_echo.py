"""Tests for SerialInterface.open()/close() echo handling.

Closes daqifi/daqifi-python-core#2: open() now sends ``SYSTem:ECHO -1``
to suppress firmware echo + DAQIFI> prompt for the rest of the session.
close() restores echo so a subsequent terminal user sees the prompt.
"""
from unittest.mock import MagicMock, patch

import pytest


# Patch pyserial at module load — SerialInterface accesses serial.Serial
# in __init__ + open() and we don't want to touch real hardware.


def _make_fake_serial(in_waiting_seq=None):
    """Return a MagicMock simulating pyserial.Serial."""
    s = MagicMock()
    s.is_open = True
    s.write = MagicMock(return_value=None)
    s.flush = MagicMock()
    s.reset_input_buffer = MagicMock()
    s.reset_output_buffer = MagicMock()
    s.read = MagicMock(return_value=b"")
    # in_waiting can be a sequence of return values across calls or
    # a single int. MagicMock treats a list as side_effect candidates.
    if in_waiting_seq is None:
        s.in_waiting = 0
    else:
        # Use PropertyMock for sequenced reads.
        from unittest.mock import PropertyMock
        type(s).in_waiting = PropertyMock(side_effect=in_waiting_seq)
    s.close = MagicMock()
    return s


def test_open_sends_disable_echo_command():
    """The first thing open() does after the port stabilizes is send
    ``SYSTem:ECHO -1`` so the firmware suppresses echo + DAQIFI> prompt
    for the rest of the session."""
    from daqifi._internal.comm import SerialInterface

    fake = _make_fake_serial()
    with patch("daqifi._internal.comm.serial.Serial", return_value=fake):
        si = SerialInterface(port="/dev/null", baudrate=115200)
        assert si.open() is True

    # Find the SYSTem:ECHO -1 command among the writes.
    written = [call.args[0] for call in fake.write.call_args_list]
    echo_writes = [b for b in written if b"ECHO -1" in b]
    assert echo_writes, f"expected SYSTem:ECHO -1 write, got: {written!r}"
    assert echo_writes[0] == b"SYSTem:ECHO -1\r"


def test_close_sends_restore_echo_command():
    """close() restores echo (SYSTem:ECHO 1) before closing so a human
    opening the port in a serial terminal afterward sees the prompt."""
    from daqifi._internal.comm import SerialInterface

    fake = _make_fake_serial()
    with patch("daqifi._internal.comm.serial.Serial", return_value=fake):
        si = SerialInterface(port="/dev/null")
        si.open()
        # Reset write history so we only see the close-time write.
        fake.write.reset_mock()
        si.close()
    written = [call.args[0] for call in fake.write.call_args_list]
    restore = [b for b in written if b"ECHO 1" in b]
    assert restore, f"expected SYSTem:ECHO 1 on close, got: {written!r}"


def test_open_succeeds_even_if_echo_disable_raises():
    """The ECHO disable is best-effort — a flaky USB-CDC enumeration
    might lose the write, but open() must still report success because
    the existing string-replace fallbacks compensate."""
    from daqifi._internal.comm import SerialInterface
    import serial as pyserial

    fake = _make_fake_serial()
    # Make ECHO -1 write raise once but the initial buffer-reset writes succeed.
    call_count = [0]

    def flaky_write(payload):
        call_count[0] += 1
        if b"ECHO -1" in payload:
            raise pyserial.SerialException("simulated transient")
        return None

    fake.write.side_effect = flaky_write
    with patch("daqifi._internal.comm.serial.Serial", return_value=fake):
        si = SerialInterface(port="/dev/null")
        # Must NOT propagate the SerialException up to the caller.
        assert si.open() is True


def test_open_drains_input_buffer_after_disable_echo():
    """Qodo PR #55 review #1: the drain loop must call reset_input_buffer
    after the polling drain so a discovery flow's *IDN? right after
    open() can't pick up stale echo bytes the polling missed."""
    from daqifi._internal.comm import SerialInterface

    fake = _make_fake_serial()
    with patch("daqifi._internal.comm.serial.Serial", return_value=fake):
        si = SerialInterface(port="/dev/null")
        si.open()
    # reset_input_buffer is called twice: once in the original open
    # path (line 1224) and once at the end of _send_disable_echo.
    assert fake.reset_input_buffer.call_count >= 2


def test_open_sets_write_timeout():
    """Qodo PR #55 review #2: serial.Serial must be constructed with a
    write_timeout so the open-time echo-disable write can't block
    indefinitely on a flaky USB CDC enumeration."""
    from daqifi._internal.comm import SerialInterface

    captured = {}

    def capture_serial(**kwargs):
        captured.update(kwargs)
        return _make_fake_serial()

    with patch("daqifi._internal.comm.serial.Serial", side_effect=capture_serial):
        si = SerialInterface(port="/dev/null", timeout=2.5)
        si.open()
    assert "write_timeout" in captured
    assert captured["write_timeout"] == 2.5


def test_close_tolerates_serial_exception_on_restore():
    """close() must not raise when the restore-echo write fails — the
    underlying port may already be gone."""
    from daqifi._internal.comm import SerialInterface
    import serial as pyserial

    fake = _make_fake_serial()
    with patch("daqifi._internal.comm.serial.Serial", return_value=fake):
        si = SerialInterface(port="/dev/null")
        si.open()
        # Re-rig write to fail on the restore.
        fake.write.side_effect = pyserial.SerialException("port gone")
        si.close()  # MUST NOT raise
    assert fake.close.called
