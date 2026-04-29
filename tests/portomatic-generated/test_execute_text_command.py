"""Tests for #22 — DaqifiDevice.execute_text_command + contains_scpi_error
+ drain_errors. The execute_text_command unit tests use fake transports
+ controlled streams so we can verify pause/resume + capture without
hardware."""
import io
import threading
import time
from unittest.mock import MagicMock

import pytest

from daqifi._internal import scpi as scpi_mod
from daqifi._internal.comm import ScpiMessage
from daqifi.devices import DaqifiStreamingDevice


# ---------------------------------------------------------------------------
# contains_scpi_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lines,expected", [
    ([], False),
    (["OK"], False),
    (["**ERROR: -200, Execution error"], True),
    (["ERROR -100"], True),
    (["  **ERROR: bad"], True),                   # leading whitespace
    (["**error: lowercase"], True),               # case-insensitive
    (["status: OK", "**ERROR: -100"], True),      # any line in list
    (["status: erroneous"], False),               # only matches at line start
])
def test_contains_scpi_error(lines, expected):
    assert scpi_mod.contains_scpi_error(lines) is expected


# ---------------------------------------------------------------------------
# drain_errors
# ---------------------------------------------------------------------------


class _FakeSerialInterface:
    """Minimum SerialInterface stand-in that returns a queue of canned
    SYST:ERR? responses then 'No error'."""

    def __init__(self, responses: list[str]):
        # Last response should be 'No error' to terminate the loop
        # (test queries the count of errors popped).
        self._responses = list(responses)
        self.send_query_calls: list[str] = []

    def send_query(self, query: str, delay: float = 0.05) -> str:
        self.send_query_calls.append(query)
        if self._responses:
            return self._responses.pop(0)
        return '0,"No error"'


def test_drain_errors_returns_popped_list():
    from daqifi._internal.comm import SerialInterface
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(side_effect=[
        '-100,"Bad command"',
        '-200,"Execution error"',
        '0,"No error"',
    ])
    popped = si.drain_errors()
    assert popped == ['-100,"Bad command"', '-200,"Execution error"']


def test_drain_errors_caps_at_max_iterations():
    from daqifi._internal.comm import SerialInterface
    si = SerialInterface(port="/dev/null")
    # Simulate device that never reports clean — caps at max_iterations.
    si.send_query = MagicMock(return_value='-100,"Bad command"')
    popped = si.drain_errors(max_iterations=5)
    # 5 real errors + the DRAIN_INCONCLUSIVE sentinel appended when
    # the loop exits without seeing 0,"No error" (added in the
    # Qodo PR #64 review pass).
    assert len(popped) == 6
    assert popped[-1].startswith("DRAIN_INCONCLUSIVE,")
    assert popped[:-1] == ['-100,"Bad command"'] * 5


def test_clear_errors_shim_still_works():
    """Backward-compat: clear_errors() exists and discards the list."""
    from daqifi._internal.comm import SerialInterface
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(side_effect=[
        '-100,"Bad command"',
        '0,"No error"',
    ])
    # Should not raise; returns None.
    assert si.clear_errors() is None


# ---------------------------------------------------------------------------
# execute_text_command — uses fake transport with a controllable stream.
# ---------------------------------------------------------------------------


class _ControlledStream:
    """Fake stream that returns bytes from a deque on read(), writes to
    a buffer on write(). Used to simulate a serial port for the
    text-command tests."""

    def __init__(self):
        self._read_buf = bytearray()
        self.written = bytearray()
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._read_buf.extend(data)

    def read(self, n: int = -1) -> bytes:
        with self._lock:
            if n < 0:
                n = len(self._read_buf)
            chunk = bytes(self._read_buf[:n])
            del self._read_buf[:n]
            return chunk

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._read_buf)

    def close(self) -> None:
        pass


class _FakeTransport:
    """Fake transport with a _ControlledStream + status callbacks."""

    def __init__(self):
        self._stream = _ControlledStream()
        self._connected = False
        self._status_handlers = []

    @property
    def stream(self):
        return self._stream

    @property
    def is_connected(self):
        return self._connected

    def add_status_changed(self, h):
        self._status_handlers.append(h)

    def remove_status_changed(self, h):
        if h in self._status_handlers:
            self._status_handlers.remove(h)

    def connect(self, retry_options=None):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def close(self):
        self._connected = False


def _make_dev_with_consumer():
    """Build a connected DaqifiStreamingDevice with the fake transport.
    Bypasses initialize() (which would send actual SCPI commands)."""
    from daqifi.devices import DaqifiStreamingDevice
    t = _FakeTransport()
    dev = DaqifiStreamingDevice(name="d", transport=t)
    dev.connect()
    return dev


def test_execute_text_command_captures_lines():
    """Feed the stream a 2-line text response after setup_action runs;
    verify the helper captures both lines."""
    dev = _make_dev_with_consumer()
    stream = dev._transport._stream

    captured_during_setup = {"value": False}

    def setup_action():
        # Simulate setup_action sending a query AND the device responding.
        # Feed text bytes directly — _on_text_line will pick them up.
        stream.feed(b"line one\r\nline two\r\n")
        captured_during_setup["value"] = True

    lines = dev.execute_text_command(
        setup_action,
        response_timeout_ms=2000,
        completion_timeout_ms=200,
    )
    assert captured_during_setup["value"] is True
    assert lines == ["line one", "line two"]
    try:
        dev.close()
    except Exception:
        pass


def test_execute_text_command_returns_empty_on_no_response():
    """If the device sends nothing within response_timeout_ms, return []."""
    dev = _make_dev_with_consumer()

    def setup_action():
        pass  # don't feed anything

    lines = dev.execute_text_command(
        setup_action,
        response_timeout_ms=200,
        completion_timeout_ms=100,
    )
    assert lines == []
    try:
        dev.close()
    except Exception:
        pass


def test_execute_text_command_restarts_protobuf_consumer_after():
    """The protobuf consumer must be running again after the text
    command returns — even if setup_action raises."""
    dev = _make_dev_with_consumer()
    assert dev._consumer.is_running

    def bad_setup():
        raise RuntimeError("simulated mid-setup failure")

    with pytest.raises(RuntimeError, match="simulated"):
        dev.execute_text_command(bad_setup, 100, 100)

    # Restarted via the finally block.
    assert dev._consumer.is_running
    try:
        dev.close()
    except Exception:
        pass


def test_execute_text_command_raises_without_consumer():
    """Helper requires an active consumer; clear it to simulate not-connected."""
    dev = _make_dev_with_consumer()
    dev._consumer = None
    with pytest.raises(RuntimeError, match="active consumer"):
        dev.execute_text_command(lambda: None, 100, 100)
