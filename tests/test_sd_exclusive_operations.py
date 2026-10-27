"""#164 item 6: SD operations run as one exchange each.

A raw SD capture (list, delete, download) didn't take the text-exchange lock,
so a second SD call on another thread switched the SPI bus under it and read
its reply off the same stream: a listing consumed a download's end-of-file
marker, the download hung and its file came out truncated."""

import importlib.util
import io
import os
import threading
import time

import pytest

from daqifi.sdcard import END_OF_FILE_MARKER

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes", os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py")
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)

_FILE = bytes(range(256)) * 16
_LISTING = b"DAQIFI> log_a.bin\r\nlog_b.bin\r\n"


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_a_listing_during_a_download_waits_for_it() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": _LISTING})
    stream = device._transport.stream
    destination = io.BytesIO()
    results = {}

    def download():
        results["download"] = device.download_sd_card_file("log_a.bin", destination)

    def listing():
        results["listing"] = device.get_sd_card_files()

    a = threading.Thread(target=download)
    a.start()
    _wait_for(lambda: any(b"SD:GET" in w for w in stream.written))
    stream.feed(b"DAQIFI> " + _FILE[:300])
    writes_before = len(stream.written)
    b = threading.Thread(target=listing)
    b.start()
    time.sleep(0.3)
    # The listing has sent nothing (no stream stop, no bus switch, no LIST?):
    # it waits for the download's exchange to finish.
    assert stream.written[writes_before:] == []
    for i in range(300, len(_FILE), 100):
        stream.feed(_FILE[i:i + 100])
        time.sleep(0.01)
    stream.feed(END_OF_FILE_MARKER)
    a.join(10)
    b.join(10)

    assert not a.is_alive() and not b.is_alive()
    assert destination.getvalue() == _FILE
    assert results["download"].file_size == len(_FILE)
    assert [f.file_name for f in results["listing"]] == ["log_a.bin", "log_b.bin"]


def test_each_operation_restores_the_bus_before_the_next_one_switches_it() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": _LISTING})
    stream = device._transport.stream
    threads = [threading.Thread(target=device.get_sd_card_files) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    sequence = [w for w in stream.written if b"LAN:ENAbled" in w]
    # Disable/enable strictly alternate: no operation's restore lands inside
    # another one's SD window.
    assert sequence == [b"SYSTem:COMMunicate:LAN:ENAbled 0\r\n", b"SYSTem:COMMunicate:LAN:ENAbled 1\r\n"] * 3


def test_a_text_command_sd_operation_runs_under_the_lock_it_holds() -> None:
    # (No prompt on the content line: the text-command path's handling of
    # "DAQIFI> name" is a separate, pre-existing matter.)
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"log_a.bin\r\nlog_b.bin\r\n"})
    device.request_sd_card_file_list()
    assert [f.file_name for f in device.sd_card_files] == ["log_a.bin", "log_b.bin"]
    assert device._text_exchange_lock_owner is None


def test_an_sd_operation_from_a_setup_action_is_refused() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": _LISTING})
    errors = []

    def setup():
        try:
            device.get_sd_card_files()
        except RuntimeError as exc:
            errors.append(str(exc))

    device.execute_text_command(setup, response_timeout_ms=200, completion_timeout_ms=50)
    assert errors and "cannot run inside another exchange" in errors[0]
