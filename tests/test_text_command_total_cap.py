"""execute_text_command's optional absolute cap (#190, audit r7).

Its timeouts measure inactivity, so a device that keeps sending lines (a
text stream left running) kept the capture going. ``max_total_ms`` bounds it.
"""

import importlib.util
import os
import time

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes", os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py")
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


def _flooding_device():
    device = _fakes._make_device_with_serial_facade()
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


def test_without_a_cap_the_inactivity_rules_are_unchanged() -> None:
    device = _fakes._make_device_with_serial_facade({b"*IDN?": b"DAQiFi,Nyquist1\r\n"})
    from daqifi._internal.comm import ScpiMessage

    lines = device.execute_text_command(
        lambda: device.send(ScpiMessage("*IDN?")), response_timeout_ms=500, completion_timeout_ms=200
    )
    assert lines == ["DAQiFi,Nyquist1"]
