"""execute_text_command's optional absolute cap (#190, audit r7).

Its timeouts measure inactivity, so a device that keeps sending lines (a
text stream left running) kept the capture going. ``max_total_ms`` bounds it.
"""

import importlib.util
import os
import time

import pytest

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes", os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py")
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)

_made = []


@pytest.fixture(autouse=True)
def _disconnect_devices():
    # A connected fake leaves a consumer thread polling its stream; the
    # flooding one would spin for the rest of the suite.
    yield
    while _made:
        try:
            _made.pop().disconnect()
        except Exception:
            pass


def _device(response_map=None):
    device = _fakes._make_device_with_serial_facade(response_map)
    _made.append(device)
    return device


def _flooding_device():
    device = _device()
    stream = device._transport.stream
    stream.feed(b"1000,0.5\r\n" * 200)
    real_read = stream.read

    def _read(n):
        stream.feed(b"1000,0.5\r\n")  # never goes quiet
        return real_read(n)

    stream.read = _read
    return device


def test_a_capture_against_a_device_that_never_goes_quiet_stops_at_the_cap() -> None:
    device = _flooding_device()
    start = time.monotonic()
    lines = device.execute_text_command(
        lambda: None, response_timeout_ms=500, completion_timeout_ms=200, max_total_ms=1000
    )
    assert time.monotonic() - start < 3.0
    assert lines


def test_the_cap_also_bounds_the_wait_for_the_first_line() -> None:
    # Audit r8: the cap only applied after the first line, so a silent
    # device still took the full response_timeout_ms.
    device = _device()
    start = time.monotonic()
    lines = device.execute_text_command(
        lambda: None, response_timeout_ms=1200, completion_timeout_ms=200, max_total_ms=50
    )
    assert lines == []
    assert time.monotonic() - start < 0.6


def test_a_first_line_later_than_the_cap_is_not_captured() -> None:
    device = _device()
    stream = device._transport.stream
    import threading

    threading.Timer(0.3, lambda: stream.feed(b"late\r\n")).start()
    lines = device.execute_text_command(
        lambda: None, response_timeout_ms=1200, completion_timeout_ms=200, max_total_ms=50
    )
    assert lines == []


@pytest.mark.parametrize("cap", [None, 5000])
def test_a_zero_timeout_still_drains_lines_already_queued(cap) -> None:
    # Audit r9 (2): get(timeout=0) used to drain what had arrived; the
    # clamp's "wait <= 0" stop dropped it, even with no cap set.
    device = _device(
        {b"*IDN?": b"first\r\nsecond\r\nthird\r\n"}
    )
    from daqifi._internal.comm import ScpiMessage

    def _setup():
        device.send(ScpiMessage("*IDN?"))
        time.sleep(0.3)  # let all three lines reach the queue

    # Without a cap the call is the original signature, so the same test
    # runs against a tree that predates max_total_ms.
    extra = {} if cap is None else {"max_total_ms": cap}
    lines = device.execute_text_command(
        _setup, response_timeout_ms=500, completion_timeout_ms=0, **extra
    )
    assert lines == ["first", "second", "third"]


def test_lines_already_queued_when_the_cap_runs_out_are_kept() -> None:
    device = _device(
        {b"*IDN?": b"first\r\nsecond\r\nthird\r\n"}
    )
    from daqifi._internal.comm import ScpiMessage

    def _setup():
        device.send(ScpiMessage("*IDN?"))
        time.sleep(0.3)

    lines = device.execute_text_command(
        _setup, response_timeout_ms=500, completion_timeout_ms=200, max_total_ms=100
    )
    assert lines == ["first", "second", "third"]


def test_without_a_cap_the_inactivity_rules_are_unchanged() -> None:
    device = _device({b"*IDN?": b"DAQiFi,Nyquist1\r\n"})
    from daqifi._internal.comm import ScpiMessage

    lines = device.execute_text_command(
        lambda: device.send(ScpiMessage("*IDN?")), response_timeout_ms=500, completion_timeout_ms=200
    )
    assert lines == ["DAQiFi,Nyquist1"]
