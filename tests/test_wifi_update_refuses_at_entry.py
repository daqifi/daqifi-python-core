"""``update_wifi_module()`` called from a message-received callback must be
refused BEFORE any wire traffic, so a refused update leaves the device's stream
running. The WiFi-path sibling of #254 (update_firmware).

Before the fix nothing refused it at the entry, and the refusal that DID exist
upstream of the flash was swallowed: ``_is_wifi_firmware_up_to_date`` calls
``get_lan_chip_info()``, which the consumer thread is refused, but it catches
the exception and answers "not up to date". The flow then entered
``PREPARING_DEVICE``, which stops streaming -- ``stop_streaming()`` and an
unconditional ``SYSTem:STReam:STOP``, sends the consumer thread wins when the
exchange is idle -- and only the following ``_send_and_settle`` refused. The
caller got an exception AND a device whose stream had been stopped.

The callback registers on the consumer's own ``add_message_received``, so the
call really runs on the protobuf consumer thread (the #238 harness). Both
``skip_version_check`` values are driven: the version check is the step that
swallowed the upstream refusal, so each path has to be shown refused at entry.

Two-sided: against the pre-fix service these FAIL -- ``SYSTem:STReam:STOP``
is on the wire and ``is_streaming`` is False. With the fix they pass: refused
at entry, no bytes, still streaming.
"""
import importlib.util
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.update_service import DefaultFirmwareUpdateService

_HERE = Path(__file__).parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_sd = _load("sd_fakes_wifi_entry",
            _HERE / "portomatic-generated" / "test_sd_card_operations_impl.py")
_f = _load("fw_fakes_wifi_entry",
           _HERE / "portomatic-generated" / "test_default_firmware_update_service.py")
# The bridge-exit suite owns the cmd.exe / not-WSL pins the WiFi flash path
# needs; reuse its fixture rather than a second copy that could drift.
_b = _load("fw_bridge_exit_wifi_entry", _HERE / "test_wifi_update_bridge_exit.py")
_pin_cmd_exe = _b._pin_cmd_exe          # autouse fixture


@pytest.fixture(autouse=True)
def _short_bridge_bound(monkeypatch):
    _f._short_wifi_bridge_bound.__wrapped__(monkeypatch)


def _frame(n: int = 2) -> bytes:
    from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage

    body = DaqifiOutMessage(analog_in_port_num=n, device_sn=1234).SerializeToString()
    return bytes([len(body)]) + body


def _wait_for(predicate, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.005)


def _service() -> DefaultFirmwareUpdateService:
    options = _f._fast_options()
    # The scripted transport has no serial port name, and without one the
    # flash request fails to build BEFORE the prepare step -- so the pre-fix
    # run would never reach the STOP this test exists to see.
    options.wifi_port_override = "TESTPORT"
    return DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=_f._FakeExternalProcessRunner(),
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=options,
    )


@pytest.mark.parametrize("skip_version_check", [False, True])
def test_update_wifi_module_from_a_callback_is_refused_before_any_wire_traffic(
        skip_version_check) -> None:
    device = _sd._make_device_with_serial_facade()
    device.start_streaming()
    assert device.is_streaming, "the device must be streaming to start with"

    service = _service()
    firmware_dir = _f._make_temp_wifi_dir()
    fired = threading.Event()
    outcome = {}

    def callback(_args):
        if fired.is_set():
            return
        outcome["thread"] = threading.current_thread()
        outcome["written_before"] = len(device._transport.stream.written)
        try:
            service.update_wifi_module(
                device, firmware_dir, skip_version_check=skip_version_check)
            outcome["result"] = ("returned", None)
        except BaseException as exc:                              # noqa: BLE001
            outcome["result"] = ("raised", exc)
        outcome["written_after"] = len(device._transport.stream.written)
        fired.set()

    device._consumer.add_message_received(callback)
    try:
        device._transport.stream.feed(_frame())
        _wait_for(fired.is_set, what="the callback to run")
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)

    assert outcome["thread"] is device._consumer._thread, (
        "the call must run on the protobuf consumer thread"
    )

    # The defect, asserted FIRST so the pre-fix run fails on it rather than on
    # the exception's type: refused BEFORE the first byte, stream still up.
    sent = b"".join(
        device._transport.stream.written[outcome["written_before"]:outcome["written_after"]]
    )
    assert sent == b"", (
        f"a refused update_wifi_module must send nothing; it sent {sent!r}"
    )
    assert device.is_streaming, (
        "a refused update_wifi_module stopped the device's stream"
    )

    kind, exc = outcome["result"]
    assert kind == "raised" and isinstance(exc, RuntimeError), (
        f"update_wifi_module from a callback must be refused, got {outcome['result']!r}"
    )
    message = str(exc)
    assert "callback" in message and "thread" in message, (
        f"the refusal must name the rule (start the update from your own "
        f"thread, not a message-received callback): {message!r}"
    )
    assert service.current_state == FirmwareUpdateState.IDLE
