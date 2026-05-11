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
    # Qodo PR #64 review pass). The sentinel is now formatted as a
    # parseable SCPI error (`-100,"DRAIN_INCONCLUSIVE: ..."`) — see
    # Qodo /improve pass 6 on PR #101.
    assert len(popped) == 6
    assert "DRAIN_INCONCLUSIVE" in popped[-1]
    assert popped[-1].startswith('-100,"DRAIN_INCONCLUSIVE')
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


# ---------------------------------------------------------------------------
# DaqifiDevice.drain_errors — port of upstream DrainErrorQueueAsync (#185).
# Uses the same _FakeTransport + _ControlledStream pattern as the
# execute_text_command tests above; drain_errors is a thin loop on top.
# ---------------------------------------------------------------------------


def _drain_errors_dev_with_replies(replies: list[bytes]):
    """Build a connected DaqifiStreamingDevice whose stream returns the
    given replies in order — one per ``send`` call. Each reply is fed
    AFTER the corresponding setup_action issues its SCPI write.
    """
    dev = _make_dev_with_consumer()
    stream = dev._transport._stream
    queue = list(replies)

    # Patch send so each call to it (one per drain loop iteration)
    # immediately enqueues the next canned reply onto the stream
    # buffer, mimicking a device that responds promptly to SYST:ERR?.
    original_send = dev.send

    def fake_send(message):
        if queue:
            stream.feed(queue.pop(0))
        # Don't actually attempt to write — the fake stream's write
        # is a no-op for these tests.
        return None

    dev.send = fake_send
    return dev, queue


def test_drain_errors_clean_queue_returns_empty():
    dev, _ = _drain_errors_dev_with_replies([b'0,"No error"\r\n'])
    popped = dev.drain_errors()
    assert popped == []
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_returns_popped_list_in_order():
    """Mirrors upstream
    DrainErrorQueueAsync_WhenSeveralErrorsQueued_ReturnsAllInOrder."""
    dev, _ = _drain_errors_dev_with_replies([
        b'-200,"Execution error"\r\n',
        b'-113,"Undefined header"\r\n',
        b'-410,"Query INTERRUPTED"\r\n',
        b'0,"No error"\r\n',
    ])
    popped = dev.drain_errors()
    assert popped == [
        '-200,"Execution error"',
        '-113,"Undefined header"',
        '-410,"Query INTERRUPTED"',
    ]
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_accepts_plus_zero_terminator():
    """Mirrors upstream
    DrainErrorQueueAsync_AcceptsPlusZeroNoErrorTerminator."""
    dev, _ = _drain_errors_dev_with_replies([
        b'-200,"Execution error"\r\n',
        b'+0,"No error"\r\n',
    ])
    popped = dev.drain_errors()
    assert popped == ['-200,"Execution error"']
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_trims_leading_and_trailing_whitespace():
    """Mirrors upstream
    DrainErrorQueueAsync_WhenReplyHasTrailingWhitespace_TrimsBeforeReturning."""
    dev, _ = _drain_errors_dev_with_replies([
        b'  -200,"Execution error"  \r\n',
        b'0,"No error"\r\n',
    ])
    popped = dev.drain_errors()
    assert popped == ['-200,"Execution error"']
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_caps_at_max_iterations_with_sentinel():
    """When the device never reports clean, cap the loop and append the
    DRAIN_INCONCLUSIVE sentinel so callers can detect 'queue not
    confirmed clean' instead of treating empty popped as success.
    The sentinel is formatted as a parseable SCPI error
    (`-100,"DRAIN_INCONCLUSIVE: ..."`) so callers piping it through
    parse_system_error see a non-zero code (Qodo /improve pass 6
    on PR #101)."""
    # 5 errors, never any "No error" terminator.
    replies = [b'-100,"Bad command"\r\n'] * 20
    dev, _ = _drain_errors_dev_with_replies(replies)
    popped = dev.drain_errors(max_iterations=5)
    assert len(popped) == 6  # 5 real + 1 sentinel
    assert popped[-1].startswith('-100,"DRAIN_INCONCLUSIVE')
    assert popped[:-1] == ['-100,"Bad command"'] * 5
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_bails_on_consecutive_empty_replies():
    """empty_budget caps the worst-case stall. Three consecutive
    empty/unparseable replies → bail with sentinel (queue inconclusive)."""
    # Stream is never fed anything — every send results in empty reply.
    dev = _make_dev_with_consumer()
    dev.send = lambda message: None  # no-op send

    popped = dev.drain_errors(
        max_iterations=256,
        empty_budget=2,
        response_timeout_ms=50,
        completion_timeout_ms=50,
    )
    # Should bail after the empty_budget — no real entries, just sentinel.
    assert len(popped) == 1
    assert popped[0].startswith('-100,"DRAIN_INCONCLUSIVE')
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_sentinel_parses_as_non_zero_scpi_error():
    """Regression: the DRAIN_INCONCLUSIVE sentinel must be formatted
    as a parseable SCPI error so callers piping popped entries
    through scpi.parse_system_error see a non-zero code. Without the
    leading numeric prefix, parse_system_error catches the ValueError
    and falls back to (0, "No error"), silently classifying the
    sentinel as a clean queue (Qodo /improve pass 6 importance 9 on
    PR #101)."""
    from daqifi._internal.scpi import parse_system_error

    # Trigger the sentinel by exhausting empty_budget.
    dev = _make_dev_with_consumer()
    dev.send = lambda message: None  # no-op send → empty replies

    popped = dev.drain_errors(
        max_iterations=10,
        empty_budget=2,
        response_timeout_ms=50,
        completion_timeout_ms=50,
    )
    assert len(popped) == 1
    sentinel = popped[0]
    code, desc = parse_system_error(sentinel)
    assert code == -100, (
        f"sentinel must parse as non-zero SCPI error so callers don't "
        f"see it as a clean queue; got code={code}, desc={desc!r}"
    )
    assert "DRAIN_INCONCLUSIVE" in desc
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_validates_max_iterations():
    dev = _make_dev_with_consumer()
    with pytest.raises(ValueError, match="max_iterations"):
        dev.drain_errors(max_iterations=0)
    with pytest.raises(ValueError, match="max_iterations"):
        dev.drain_errors(max_iterations=-5)
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_validates_empty_budget():
    dev = _make_dev_with_consumer()
    with pytest.raises(ValueError, match="empty_budget"):
        dev.drain_errors(empty_budget=0)
    with pytest.raises(ValueError, match="empty_budget"):
        dev.drain_errors(empty_budget=-1)
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_skips_daqifi_prompt_prefix():
    """Regression: drain_errors must not be derailed by a leading
    `DAQIFI>` prompt that prefixes (or precedes) the actual SCPI
    reply on noisy streams. From Qodo /agentic_review on PR #101 —
    the prior code picked only the first non-empty line and treated
    a prompt-only line as garbage, prematurely bailing via
    empty_budget."""
    # Each capture has a leading "DAQIFI>" line BEFORE the real
    # SCPI error reply. The drain must skip past the prompt and
    # find the parseable reply.
    dev, _ = _drain_errors_dev_with_replies([
        b'DAQIFI>\r\n-200,"Execution error"\r\n',
        b'DAQIFI>\r\n0,"No error"\r\n',
    ])
    popped = dev.drain_errors()
    assert popped == ['-200,"Execution error"']
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_skips_inline_daqifi_prompt_prefix():
    """Same regression but the prompt is inline (`DAQIFI> -200,...`
    on the same line) instead of a separate line."""
    dev, _ = _drain_errors_dev_with_replies([
        b'DAQIFI> -113,"Undefined header"\r\n',
        b'DAQIFI> 0,"No error"\r\n',
    ])
    popped = dev.drain_errors()
    assert popped == ['-113,"Undefined header"']
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_raises_when_not_connected():
    """The docstring promises RuntimeError when the device isn't
    connected. Verify the precondition fires before any IO is
    attempted (Qodo /improve pass 4 importance 7)."""
    from daqifi.devices import DaqifiStreamingDevice
    t = _FakeTransport()
    dev = DaqifiStreamingDevice(name="d", transport=t)
    # NOT calling dev.connect() — should fail the is_connected check.
    with pytest.raises(RuntimeError, match="not connected"):
        dev.drain_errors()


def test_drain_errors_strips_lowercase_daqifi_prompt():
    """Regression: case-insensitive prompt strip — firmware variants
    may emit `Daqifi>` / `daqifi>` instead of canonical `DAQIFI>`."""
    dev, _ = _drain_errors_dev_with_replies([
        b'daqifi>\r\n-200,"Execution error"\r\n',
        b'Daqifi> 0,"No error"\r\n',
    ])
    popped = dev.drain_errors()
    assert popped == ['-200,"Execution error"']
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_accepts_unquoted_no_error_terminator():
    """Slightly-nonconforming firmware that returns `0,No error`
    (description not quoted) must still be recognized as a clean-queue
    terminator. Real errors keep the strict-quote requirement."""
    dev, _ = _drain_errors_dev_with_replies([
        b'-100,"Bad"\r\n',
        b'0,No error\r\n',  # unquoted terminator
    ])
    popped = dev.drain_errors()
    assert popped == ['-100,"Bad"']
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_accepts_unquoted_error_descriptions():
    """Regression: real firmware variants emit unquoted error
    descriptions like `-200,Execution error`. The drain must accept
    these — same behavior as SerialInterface.drain_errors and
    scpi.parse_system_error. (An earlier strict-quote requirement
    was found via Qodo /agentic_review pass 4 to silently reject
    these, defeating the drain's purpose; corrected here.)"""
    dev, _ = _drain_errors_dev_with_replies([
        b'-200,Execution error\r\n',         # unquoted
        b'-113,"Undefined header"\r\n',      # quoted
        b'0,"No error"\r\n',
    ])
    popped = dev.drain_errors()
    assert popped == [
        '-200,Execution error',
        '-113,"Undefined header"',
    ]
    try:
        dev.close()
    except Exception:
        pass


def test_drain_errors_validates_timeouts():
    dev = _make_dev_with_consumer()
    with pytest.raises(ValueError, match="response_timeout_ms"):
        dev.drain_errors(response_timeout_ms=0)
    with pytest.raises(ValueError, match="response_timeout_ms"):
        dev.drain_errors(response_timeout_ms=-100)
    with pytest.raises(ValueError, match="completion_timeout_ms"):
        dev.drain_errors(completion_timeout_ms=0)
    with pytest.raises(ValueError, match="completion_timeout_ms"):
        dev.drain_errors(completion_timeout_ms=-50)
    try:
        dev.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# NyquistDevice.drain_errors — facade delegates to SerialInterface.drain_errors.
# ---------------------------------------------------------------------------


def test_nyquistdevice_drain_errors_delegates_to_serial_interface():
    """NyquistDevice.drain_errors is a thin pass-through to
    SerialInterface.drain_errors. Unit-test by constructing an instance
    with a mocked _comm and verifying the call shape."""
    from daqifi.device import NyquistDevice

    dev = NyquistDevice.__new__(NyquistDevice)  # bypass __init__
    dev._comm = MagicMock()
    dev._comm.drain_errors.return_value = [
        '-100,"Bad"',
        '-200,"Worse"',
    ]

    out = dev.drain_errors(max_iterations=42, empty_budget=7)
    assert out == ['-100,"Bad"', '-200,"Worse"']
    dev._comm.drain_errors.assert_called_once_with(
        max_iterations=42, empty_budget=7
    )


def test_nyquistdevice_drain_errors_wraps_transport_error_as_command_error():
    """When the underlying SerialInterface.drain_errors raises (e.g. a
    transport-level failure), NyquistDevice.drain_errors must surface
    it as a CommandError — matches the get_last_error facade pattern."""
    from daqifi.device import NyquistDevice
    from daqifi.exceptions import CommandError

    dev = NyquistDevice.__new__(NyquistDevice)  # bypass __init__
    dev._comm = MagicMock()
    dev._comm.drain_errors.side_effect = OSError("serial port closed")

    with pytest.raises(CommandError, match="Failed to drain error queue"):
        dev.drain_errors()


def test_nyquistdevice_drain_errors_validates_args_before_try_except():
    """ValueError from invalid args must propagate as ValueError, NOT
    wrapped into CommandError — Qodo /improve pass 3 importance 7."""
    from daqifi.device import NyquistDevice

    dev = NyquistDevice.__new__(NyquistDevice)
    dev._comm = MagicMock()
    with pytest.raises(ValueError, match="max_iterations"):
        dev.drain_errors(max_iterations=0)
    with pytest.raises(ValueError, match="empty_budget"):
        dev.drain_errors(empty_budget=0)
    # Underlying _comm should never be called when validation fails.
    dev._comm.drain_errors.assert_not_called()
