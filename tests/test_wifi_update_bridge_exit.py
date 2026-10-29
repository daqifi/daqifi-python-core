"""#190 items 1-4: the WiFi module update and the USB-transparent bridge.

LAN:FWUpdate only requests the update (SCPI_LANFwUpdate sets
WIFI_FW_UPDATE_REQUESTED); the next LAN:APPLY enters the bridge, which forwards
the USB CDC port raw to the WINC (UsbCdc_SetTransparentMode(true)); only an
exact ``SYSTem:USB:SetTransparentMode 0`` reaches the console again
(UsbCdc.c, UNSET_TRANSPARENT_FORMS), and APPLY from READY leaves the bridge.
"""

import importlib.util
import os
import shutil
import threading
import time

import pytest

from daqifi.firmware.update_options import FirmwareUpdateServiceOptions
from daqifi.firmware.external_process import ExternalProcessResult
from daqifi.firmware.update_events import FirmwareUpdateException
from daqifi.firmware import update_service
from daqifi.firmware.update_service import DefaultFirmwareUpdateService

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "fw_fakes",
    os.path.join(_HERE, "portomatic-generated", "test_default_firmware_update_service.py"),
)
_f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_f)

POWER_ON = "SYSTem:POWer:STATe 1"
FW_UPDATE = "SYSTem:COMMUnicate:LAN:FWUpdate"
BRIDGE_EXIT = "SYSTem:USB:SetTransparentMode 0"
LAN_ENABLE = "SYSTem:COMMunicate:LAN:ENAbled 1"
LAN_APPLY = "SYSTem:COMMunicate:LAN:APPLY"
LAN_SAVE = "SYSTem:COMMunicate:LAN:SAVE"
SUCCESS = "Operation completed successfully"


@pytest.fixture(autouse=True)
def _pin_cmd_exe(monkeypatch):
    # Same pins as the generated suite: a fixed cmd.exe, and the host not
    # treated as WSL (whose cmd.exe can't run from a Linux path).
    monkeypatch.setattr(update_service, "_running_under_wsl", lambda: False)
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: _f._FAKE_CMD_EXE if name == "cmd.exe" else real_which(name, *a, **k),
    )


def _options():
    # Built without the new fields' keywords, so the same tests run against
    # a tree that predates them (where setattr just adds unused attributes).
    options = FirmwareUpdateServiceOptions(
        poll_interval=0.02,
        preparing_device_timeout=2.0,
        programming_timeout=5.0,
        verifying_timeout=2.0,
        post_lan_firmware_mode_delay=0.005,
        post_wifi_reconnect_delay=0.005,
        wifi_process_timeout=2.0,
    )
    options.power_on_wifi_module_settle_delay = 0.005
    options.post_lan_disconnect_port_release_delay = 0.005
    options.post_usb_transparent_mode_exit_delay = 0.005
    # The flash tool runs for minutes on a real board, long after the bridge
    # is up; here the bridge has to be up before the fake tool "succeeds".
    options.post_lan_update_apply_delay = 0.05
    # Longer than the model's bridge entry, as the real default is longer
    # than the firmware's.
    options.wifi_bridge_entry_time = 0.1
    return options


def _result(lines=(SUCCESS,), exit_code=0):
    return ExternalProcessResult(
        exit_code=exit_code,
        timed_out=False,
        duration_seconds=0.01,
        standard_output_lines=tuple(lines),
        standard_error_lines=tuple(),
    )


def _update(device, runner, cancel_event=None):
    service = DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=runner,
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=_options(),
    )
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        service.update_wifi_module(device, firmware_dir, cancel_event=cancel_event)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)


class _FirmwareModel(_f._FakeStreamingDevice):
    """A device that follows the firmware's WiFi-update transitions.

    wifi_manager.c / SCPILAN.c / UsbCdc.c (daqifi-nyquist-firmware main):
    FWUpdate sets REQUESTED; APPLY enters the bridge from REQUESTED (READY
    and transparent after the WINC init), leaves it from READY, and only
    reinitialises otherwise; while transparent, everything but
    SetTransparentMode 0 goes raw to the WINC.
    """

    ENTRY_DELAY = 0.03

    def __init__(self, name="fake-port", deliver_then_raise=None, drop=None):
        super().__init__(name)
        self.state = "normal"
        self.ready_at = None
        self.transparent = False
        self.raw_to_winc = []
        self._deliver_then_raise = deliver_then_raise
        self._drop = drop

    def _settle(self):
        if self.state == "entering" and time.monotonic() >= self.ready_at:
            self.state, self.transparent = "ready", True

    def send(self, message):
        text = getattr(message, "data", None) or str(message)
        if text == self._drop:
            self._drop = None
            raise OSError("write failed before delivery")
        self.sent_commands.append(text)
        self._settle()
        if self.transparent and text != BRIDGE_EXIT:
            self.raw_to_winc.append(text)
        elif text == BRIDGE_EXIT:
            self.transparent = False
        elif text == FW_UPDATE and self.state == "normal":
            self.state = "requested"
        elif text == LAN_APPLY:
            if self.state in ("requested", "entering"):
                self.state, self.ready_at = "entering", time.monotonic() + self.ENTRY_DELAY
            elif self.state == "ready":
                self.state = "normal"
        if text == self._deliver_then_raise:
            self._deliver_then_raise = None
            raise OSError("flush failed after delivery")

    def final_state(self):
        time.sleep(self.ENTRY_DELAY * 2)
        self._settle()
        return self.state, self.transparent


def test_a_successful_flash_enters_the_bridge_and_leaves_it_before_restoring_lan() -> None:
    device = _FirmwareModel()
    _update(device, _f._FakeExternalProcessRunner(_result()))
    assert device.sent_commands == [
        POWER_ON, FW_UPDATE, LAN_APPLY, BRIDGE_EXIT, LAN_ENABLE, LAN_APPLY, LAN_SAVE,
    ]
    assert device.final_state() == ("normal", False)
    assert device.raw_to_winc == []


@pytest.mark.parametrize(
    "result",
    [
        _result(lines=(), exit_code=0),
        _result(lines=("begin write operation", "Error: could not open port"), exit_code=0),
    ],
    ids=["exit-0-no-output", "exit-0-other-failure-line"],
)
def test_success_needs_the_tools_own_success_line(result) -> None:
    device = _FirmwareModel()
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(result))
    assert SUCCESS in str(excinfo.value.__cause__)
    assert device.final_state() == ("normal", False)


def test_a_failed_flash_leaves_the_bridge_without_persisting() -> None:
    device = _FirmwareModel()
    with pytest.raises(FirmwareUpdateException):
        _update(device, _f._FakeExternalProcessRunner(_result(lines=("x",), exit_code=1)))
    assert device.final_state() == ("normal", False)
    assert LAN_SAVE not in device.sent_commands
    assert device.is_connected


def test_a_cancel_between_fwupdate_and_apply_does_not_leave_it_bridged() -> None:
    # Audit r1 (1): recovery sent T0 then APPLY, and from REQUESTED that
    # APPLY enters the bridge.
    cancel = threading.Event()
    device = _FirmwareModel()
    real_send = device.send

    def _send(message):
        real_send(message)
        if (getattr(message, "data", None) or str(message)) == FW_UPDATE:
            cancel.set()

    device.send = _send
    with pytest.raises(FirmwareUpdateException):
        _update(device, _f._FakeExternalProcessRunner(_result()), cancel_event=cancel)
    assert device.final_state() == ("normal", False)
    assert device.raw_to_winc == []


@pytest.mark.parametrize("command", [FW_UPDATE, LAN_APPLY])
def test_a_send_that_raises_after_delivery_is_still_recovered(command) -> None:
    # Audit r1 (2): the flag was armed only after send() returned.
    device = _FirmwareModel(deliver_then_raise=command)
    with pytest.raises(FirmwareUpdateException):
        _update(device, _f._FakeExternalProcessRunner(_result()))
    assert device.final_state() == ("normal", False)


def test_an_apply_that_never_arrived_is_still_safe() -> None:
    # Armed before the send, so recovery runs from REQUESTED.
    device = _FirmwareModel(drop=LAN_APPLY)
    with pytest.raises(FirmwareUpdateException):
        _update(device, _f._FakeExternalProcessRunner(_result()))
    assert device.final_state() == ("normal", False)


def test_a_failure_before_update_mode_sends_no_bridge_exit(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    device = _FirmwareModel()
    with pytest.raises(FirmwareUpdateException):
        _update(device, _f._FakeExternalProcessRunner(_result()))
    assert device.sent_commands == []


def test_a_device_that_never_returns_keeps_the_original_failure() -> None:
    device = _FirmwareModel()
    device.connect_failures_before_success = 10**6
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(_result(lines=(), exit_code=1)))
    assert "exited with code 1" in str(excinfo.value.__cause__)
    assert BRIDGE_EXIT not in device.sent_commands


def test_power_on_can_be_turned_off() -> None:
    device = _f._FakeStreamingDevice("fake-port-7")
    options = _options()
    options.power_on_wifi_module_before_lan_update_mode = False
    service = DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=_f._FakeExternalProcessRunner(_result()),
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=options,
    )
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        service.update_wifi_module(device, firmware_dir)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)
    assert device.sent_commands[:2] == [FW_UPDATE, LAN_APPLY]


@pytest.mark.parametrize(
    "field, value",
    [
        ("power_on_wifi_module_settle_delay", -1.0),
        ("post_lan_disconnect_port_release_delay", -0.1),
        ("post_usb_transparent_mode_exit_delay", -0.1),
        ("wifi_flash_success_marker", " "),
        ("post_lan_update_apply_delay", -0.1),
        ("wifi_bridge_entry_time", -1.0),
    ],
)
def test_options_reject_invalid_bridge_settings(field, value) -> None:
    options = FirmwareUpdateServiceOptions()
    setattr(options, field, value)
    with pytest.raises(ValueError, match=field):
        options.validate()


def test_zero_delays_are_allowed() -> None:
    options = _options()
    options.power_on_wifi_module_settle_delay = 0.0
    options.post_lan_disconnect_port_release_delay = 0.0
    options.post_usb_transparent_mode_exit_delay = 0.0
    options.validate()
