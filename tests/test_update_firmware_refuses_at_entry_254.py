"""#254: ``update_firmware()`` called from a message-received callback must be
refused BEFORE any wire traffic, so a refused update leaves the device's stream
running.

Before the fix the refusal was incidental: ``PREPARING_DEVICE`` called
``stop_streaming()`` first, whose send try-acquires the write exchange on the
consumer thread and succeeds when it is idle, and only the following
``_send_and_settle`` -> ``run_exclusive`` refused. The caller got a
``RuntimeError`` AND a device whose stream had been stopped.

The callback registers on the consumer's own ``add_message_received``, the
event #254 is about, so the call really runs on the protobuf consumer thread.
The harness is #238's (``test_consumer_thread_exchange_238.py``).

Two-sided: against the pre-fix service this test FAILS -- the refusal is
raised, but ``SYSTem:STReam:STOP`` is on the wire and ``is_streaming`` is
False. With the fix it passes: same refusal, no bytes, still streaming.
"""
import importlib.util
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.bootloader import Pic32BootloaderProtocol
from daqifi.firmware.update_service import DefaultFirmwareUpdateService

_HERE = Path(__file__).parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, _HERE / "portomatic-generated" / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_sd = _load("sd_fakes_254", "test_sd_card_operations_impl.py")
_svc = _load("service_fakes_254", "test_default_firmware_update_service.py")


@pytest.fixture(autouse=True)
def _no_application_ports_on_the_host(monkeypatch):
    # Whatever boards this host has attached must not decide the outcome.
    import daqifi.firmware.update_service as update_service

    monkeypatch.setattr(update_service, "_list_usb_serial_ports", lambda vid, pid: [])


def _frame(n: int = 2) -> bytes:
    from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage

    body = DaqifiOutMessage(analog_in_port_num=n, device_sn=1234).SerializeToString()
    return bytes([len(body)]) + body


def _wait_for(predicate, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.005)


def _hex_file() -> str:
    """A minimal valid image, so the call reaches the device-touching steps
    if nothing refuses it first."""
    def line(record: bytes) -> str:
        return ":" + (record + bytes([(-sum(record)) & 0xFF])).hex().upper()

    lines = [
        line(bytes([2, 0, 0, 4, 0x1D, 0x00])),
        line(bytes([8, 0x04, 0x80, 0]) + bytes(range(8))),
        ":00000001FF",
    ]
    fd, path = tempfile.mkstemp(suffix=".hex")
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def _service() -> DefaultFirmwareUpdateService:
    options = _svc._fast_options()
    options.wifi_recovery_timeout = 40.0  # validated even for a PIC32 update
    return DefaultFirmwareUpdateService(
        hid_transport=_svc._FakeHidTransport(),
        firmware_download_service=_svc._FakeFirmwareDownloadService(),
        external_process_runner=_svc._FakeExternalProcessRunner(),
        bootloader_protocol=Pic32BootloaderProtocol(),
        hid_device_enumerator=_svc._FakeHidEnumerator([]),
        options=options,
    )


def test_update_firmware_from_a_callback_is_refused_before_any_wire_traffic() -> None:
    device = _sd._make_device_with_serial_facade()
    device.start_streaming()
    assert device.is_streaming, "the device must be streaming to start with"

    service = _service()
    path = _hex_file()
    fired = threading.Event()
    outcome = {}

    def callback(_args):
        if fired.is_set():
            return
        outcome["thread"] = threading.current_thread()
        outcome["written_before"] = len(device._transport.stream.written)
        try:
            service.update_firmware(device, path)
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
        os.remove(path)

    assert outcome["thread"] is device._consumer._thread, (
        "the call must run on the protobuf consumer thread"
    )
    # The point of #254, asserted FIRST so the pre-fix run fails on the
    # defect itself rather than on the exception's type: refused BEFORE the
    # first byte, with the stream still running.
    sent = b"".join(
        device._transport.stream.written[outcome["written_before"]:outcome["written_after"]]
    )
    assert sent == b"", (
        f"a refused update_firmware must send nothing; it sent {sent!r}"
    )
    assert device.is_streaming, (
        "a refused update_firmware stopped the device's stream"
    )

    # The refusal itself: raised at the entry as a RuntimeError naming the
    # rule. (Pre-fix it surfaced from inside PREPARING_DEVICE, wrapped in a
    # FirmwareUpdateException, with the service left FAILED.)
    kind, exc = outcome["result"]
    assert kind == "raised" and isinstance(exc, RuntimeError), (
        f"update_firmware from a callback must be refused, got {outcome['result']!r}"
    )
    message = str(exc)
    assert "callback" in message and "thread" in message, (
        f"the refusal must name the rule (start the update from your own "
        f"thread, not a message-received callback): {message!r}"
    )
    assert service.current_state == FirmwareUpdateState.IDLE
