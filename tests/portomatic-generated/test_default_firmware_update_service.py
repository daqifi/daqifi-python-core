"""Unit tests for DefaultFirmwareUpdateService (the concrete orchestrator).

Mirrors ``Daqifi.Core.Tests/Firmware/FirmwareUpdateServiceTests.cs``.
Hardware-side fakes plug into the same Protocol surfaces as the
production HID transport, HID enumerator, external-process runner, and
streaming device — no real hardware required.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from datetime import timedelta
from typing import List, Optional, Sequence

import pytest

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.bootloader import BootloaderProtocol, Pic32BootloaderProtocol
from daqifi.firmware.external_process import (
    ExternalProcessRequest,
    ExternalProcessResult,
)
from daqifi.firmware.lan_chip_info import LanChipInfo
from daqifi.firmware.release_info import FirmwareReleaseInfo
from daqifi.firmware.update_events import (
    FirmwareUpdateException,
    FirmwareUpdateProgress,
    FirmwareUpdateStateChangedEventArgs,
)
from daqifi.firmware.update_options import FirmwareUpdateServiceOptions
from daqifi.firmware.update_service import DefaultFirmwareUpdateService
from daqifi.firmware.version import FirmwareVersion
from daqifi._internal.comm import HidDeviceInfo


pytestmark = pytest.mark.portomatic_generated


@pytest.fixture(autouse=True)
def _pin_not_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    # Under WSL the service translates paths for cmd.exe and refuses bundles
    # on the Linux filesystem, which is where these tests' temp directories
    # live. Pinned so every host takes the same branch; the WSL tests opt in.
    import daqifi.firmware.update_service as update_service

    monkeypatch.setattr(update_service, "_running_under_wsl", lambda: False)


# ---- Fakes ---------------------------------------------------------------


class _FakeHidTransport:
    def __init__(self) -> None:
        self.is_connected = False
        self.writes: List[bytes] = []
        self._reads: List[bytes] = []
        self._connect_calls = 0
        self._disconnect_calls = 0

    def enqueue_read(self, data: bytes) -> None:
        self._reads.append(bytes(data))

    @property
    def vendor_id(self) -> Optional[int]:
        return None

    @property
    def product_id(self) -> Optional[int]:
        return None

    @property
    def serial_number(self) -> Optional[str]:
        return None

    @property
    def device_path(self) -> Optional[str]:
        return None

    @property
    def read_timeout(self) -> timedelta:
        return timedelta(seconds=1)

    @read_timeout.setter
    def read_timeout(self, value: timedelta) -> None:
        return

    @property
    def write_timeout(self) -> timedelta:
        return timedelta(seconds=1)

    @write_timeout.setter
    def write_timeout(self, value: timedelta) -> None:
        return

    def connect(
        self,
        vendor_id: int,
        product_id: int,
        serial_number: Optional[str] = None,
        device_path=None,
    ) -> None:
        self._connect_calls += 1
        self.connected_path = device_path
        self.is_connected = True

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def read(self, timeout: Optional[timedelta] = None) -> bytes:
        if not self._reads:
            raise TimeoutError("No queued HID read.")
        return self._reads.pop(0)

    def disconnect(self) -> None:
        self._disconnect_calls += 1
        self.is_connected = False

    def close(self) -> None:
        self.disconnect()


class _FakeHidEnumerator:
    """Returns a queued list of device-batches per ``enumerate`` call."""

    def __init__(self, batches: Sequence[Sequence[HidDeviceInfo]]) -> None:
        self._batches = list(batches)

    def enumerate(
        self,
        vendor_id: Optional[int] = None,
        product_id: Optional[int] = None,
    ) -> Sequence[HidDeviceInfo]:
        if not self._batches:
            return []
        return list(self._batches.pop(0))


class _ThrowingHidEnumerator:
    def __init__(self, exception: BaseException, succeed_first: int = 0) -> None:
        self._exception = exception
        self._successes_left = succeed_first

    def enumerate(
        self,
        vendor_id: Optional[int] = None,
        product_id: Optional[int] = None,
    ) -> Sequence[HidDeviceInfo]:
        if self._successes_left > 0:
            self._successes_left -= 1
            return []
        raise self._exception


class _FakeBootloaderProtocol(BootloaderProtocol):
    def __init__(self, hex_records: Sequence[bytes]) -> None:
        self._hex_records = [bytes(r) for r in hex_records]

    def create_request_version_message(self) -> bytes:
        return bytes([0x11])

    def create_erase_flash_message(self) -> bytes:
        return bytes([0x22])

    def create_program_flash_message(self, hex_record: bytes) -> bytes:
        return bytes([0x33]) + bytes(hex_record)

    def create_jump_to_application_message(self) -> bytes:
        return bytes([0x44])

    def decode_version_response(self, data: bytes) -> str:
        if not data or len(data) < 2 or data[0] != 0x01:
            return "Error"
        return f"{data[1]}.0"

    def decode_program_flash_response(self, data: bytes) -> bool:
        return bool(data) and len(data) >= 2 and data[0] == 0x01 and data[1] == 0x03

    def decode_erase_flash_response(self, data: bytes) -> bool:
        return bool(data) and len(data) >= 2 and data[0] == 0x01 and data[1] == 0x02

    def parse_hex_file(self, hex_file_lines):
        return list(self._hex_records)


class _FakeExternalProcessRunner:
    def __init__(
        self, next_result: Optional[ExternalProcessResult] = None
    ) -> None:
        self.next_result = next_result if next_result is not None else (
            ExternalProcessResult(
                exit_code=0,
                timed_out=False,
                duration_seconds=0.01,
                standard_output_lines=("Operation completed successfully",),
                standard_error_lines=tuple(),
            )
        )
        self.last_request: Optional[ExternalProcessRequest] = None

    def run(self, request: ExternalProcessRequest) -> ExternalProcessResult:
        self.last_request = request
        if request.on_standard_output_line is not None:
            for line in self.next_result.standard_output_lines:
                request.on_standard_output_line(line)
        return self.next_result


class _FakeFirmwareDownloadService:
    def __init__(
        self, latest_wifi_release: Optional[FirmwareReleaseInfo] = None
    ) -> None:
        self.latest_wifi_release = latest_wifi_release

    async def get_latest_release(self, include_pre_release: bool = False):
        return None

    async def check_for_update(
        self, device_version_string: str, include_pre_release: bool = False
    ):
        raise NotImplementedError

    async def download_latest_firmware(
        self, destination_directory, include_pre_release: bool = False, progress=None
    ):
        return None

    async def download_firmware_by_tag(
        self, tag_name, destination_directory, progress=None
    ):
        return None

    async def download_wifi_firmware(self, destination_directory, progress=None):
        return None

    async def get_latest_wifi_release(self):
        return self.latest_wifi_release

    def invalidate_cache(self) -> None:
        return


class _FakeStreamingDevice:
    def __init__(self, name: str = "fake-port-3") -> None:
        self.name = name
        self.is_connected = True
        self.is_streaming = False
        self.connect_attempts = 0
        self.disconnect_calls = 0
        self.connect_failures_before_success = 0
        self.sent_commands: List[str] = []

    def connect(self) -> None:
        self.connect_attempts += 1
        if self.connect_failures_before_success > 0:
            self.connect_failures_before_success -= 1
            raise IOError("Simulated serial reconnect failure.")
        self.is_connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False

    def send(self, message) -> None:
        text = getattr(message, "data", None) or str(message)
        self.sent_commands.append(text)

    def stop_streaming(self) -> None:
        self.is_streaming = False

    def execute_text_command(
        self, setup_action, response_timeout_ms=5000, completion_timeout_ms=1000,
        max_total_ms=None,
    ):
        # Silent until the bridge exit has been sent: to the WiFi update the
        # bridge is up as soon as LAN:APPLY goes out, and the console
        # answers *IDN? again once SetTransparentMode 0 has.
        before = len(self.sent_commands)
        setup_action()
        sent = self.sent_commands[before:]
        exited = "SYSTem:USB:SetTransparentMode 0" in self.sent_commands
        if exited and "*IDN?" in sent:
            return ["DAQiFi,Nyquist1,0,3.8.0"]
        return []


class _FakeLanChipInfoStreamingDevice(_FakeStreamingDevice):
    def __init__(self, name: str, chip_info: LanChipInfo) -> None:
        super().__init__(name)
        self._chip_info = chip_info

    def get_lan_chip_info(self) -> Optional[LanChipInfo]:
        return self._chip_info


# ---- Test scaffolding ----------------------------------------------------


@pytest.fixture(autouse=True)
def _short_wifi_bridge_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    # The WiFi bridge watch is a firmware bound (40 s) and the recovery
    # timeout can't be set below it. These fakes have no bridge to wait
    # for, so shrink the bound, letting _fast_options keep recovery short.
    import daqifi.firmware.update_options as _update_options
    import daqifi.firmware.update_service as _update_service

    monkeypatch.setattr(_update_options, "WIFI_BRIDGE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(_update_service, "WIFI_BRIDGE_SETTLE_SECONDS", 0.05)


def _fast_options() -> FirmwareUpdateServiceOptions:
    return FirmwareUpdateServiceOptions(
        poll_interval=0.02,
        preparing_device_timeout=2.0,
        waiting_for_bootloader_timeout=2.0,
        connecting_timeout=2.0,
        erasing_flash_timeout=2.0,
        programming_timeout=5.0,
        verifying_timeout=2.0,
        jumping_to_application_timeout=2.0,
        bootloader_response_timeout=0.25,
        post_force_boot_delay=0.005,
        post_lan_firmware_mode_delay=0.005,
        post_wifi_reconnect_delay=0.005,
        hid_connect_retry_delay=0.005,
        flash_write_retry_delay=0.005,
        wifi_process_timeout=2.0,
        power_on_wifi_module_settle_delay=0.005,
        post_lan_disconnect_port_release_delay=0.005,
        post_usb_transparent_mode_exit_delay=0.005,
        wifi_recovery_timeout=0.2,
    )


def _make_temp_hex() -> str:
    fd, path = tempfile.mkstemp(suffix=".hex")
    with os.fdopen(fd, "w") as handle:
        handle.write("test")
    return path


def _make_temp_directory() -> str:
    return tempfile.mkdtemp(prefix="firmware-tests-")


def _make_temp_wifi_dir() -> str:
    path = _make_temp_directory()
    with open(os.path.join(path, "winc_flash_tool.cmd"), "w") as handle:
        handle.write("@echo off")
    return path


def _build_pic32_service(
    hid_transport: _FakeHidTransport,
    enumerator: _FakeHidEnumerator,
    bootloader_protocol: _FakeBootloaderProtocol,
    options: Optional[FirmwareUpdateServiceOptions] = None,
) -> DefaultFirmwareUpdateService:
    return DefaultFirmwareUpdateService(
        hid_transport=hid_transport,
        firmware_download_service=_FakeFirmwareDownloadService(),
        external_process_runner=_FakeExternalProcessRunner(),
        bootloader_protocol=bootloader_protocol,
        hid_device_enumerator=enumerator,
        options=options if options is not None else _fast_options(),
    )


from daqifi.firmware.update_service import (  # noqa: E402  (before the fixture patches it)
    _list_usb_serial_ports as _real_list_usb_serial_ports,
)


@pytest.fixture(autouse=True)
def _no_application_ports_on_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # The several-boards refusal lists the host's real serial ports; a board
    # plugged into the machine running the suite would otherwise refuse
    # every update test. Tests of the refusal override this.
    import daqifi.firmware.update_service as update_service

    monkeypatch.setattr(update_service, "_list_usb_serial_ports", lambda vid, pid: [])


# ---- Construction validation --------------------------------------------


class TestConstruction:
    def test_rejects_none_hid_transport(self) -> None:
        with pytest.raises(TypeError):
            DefaultFirmwareUpdateService(
                hid_transport=None,
                firmware_download_service=_FakeFirmwareDownloadService(),
                external_process_runner=_FakeExternalProcessRunner(),
            )

    def test_rejects_none_download_service(self) -> None:
        with pytest.raises(TypeError):
            DefaultFirmwareUpdateService(
                hid_transport=_FakeHidTransport(),
                firmware_download_service=None,
                external_process_runner=_FakeExternalProcessRunner(),
            )

    def test_rejects_none_process_runner(self) -> None:
        with pytest.raises(TypeError):
            DefaultFirmwareUpdateService(
                hid_transport=_FakeHidTransport(),
                firmware_download_service=_FakeFirmwareDownloadService(),
                external_process_runner=None,
            )

    def test_validates_options_on_construction(self) -> None:
        bad_options = FirmwareUpdateServiceOptions(poll_interval=-0.1)
        with pytest.raises(ValueError):
            DefaultFirmwareUpdateService(
                hid_transport=_FakeHidTransport(),
                firmware_download_service=_FakeFirmwareDownloadService(),
                external_process_runner=_FakeExternalProcessRunner(),
                options=bad_options,
            )

    def test_initial_state_is_idle(self) -> None:
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
        )
        assert service.current_state == FirmwareUpdateState.IDLE


# ---- update_firmware happy path -----------------------------------------


class TestUpdateFirmwareHappyPath:
    def test_transitions_through_expected_states_and_reports_progress(
        self,
    ) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        # version → erase ack → 2x program ack → verify version
        hid.enqueue_read(bytes([0x01, 0x10]))
        hid.enqueue_read(bytes([0x01, 0x02]))
        hid.enqueue_read(bytes([0x01, 0x03]))
        hid.enqueue_read(bytes([0x01, 0x03]))
        hid.enqueue_read(bytes([0x01, 0x10]))

        info = HidDeviceInfo(0x04D8, 0x003C, "path-1", "SN-1", "Bootloader")
        enumerator = _FakeHidEnumerator([[], [info]])
        protocol = _FakeBootloaderProtocol(
            [bytes([0xA1, 0x01]), bytes([0xA1, 0x02])]
        )
        service = _build_pic32_service(hid, enumerator, protocol)

        state_transitions: List[FirmwareUpdateState] = []
        service.state_changed = lambda args: state_transitions.append(
            args.current_state
        )
        progress_events: List[FirmwareUpdateProgress] = []

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(
                device, hex_path, progress=progress_events.append
            )
        finally:
            os.remove(hex_path)

        assert service.current_state == FirmwareUpdateState.COMPLETE
        assert state_transitions == [
            FirmwareUpdateState.PREPARING_DEVICE,
            FirmwareUpdateState.WAITING_FOR_BOOTLOADER,
            FirmwareUpdateState.CONNECTING,
            FirmwareUpdateState.ERASING_FLASH,
            FirmwareUpdateState.PROGRAMMING,
            FirmwareUpdateState.VERIFYING,
            FirmwareUpdateState.JUMPING_TO_APP,
            FirmwareUpdateState.COMPLETE,
        ]
        assert device.sent_commands == ["SYSTem:FORceBoot"]
        assert len(hid.writes) == 6  # version + erase + 2 programs + verify + jump
        assert device.disconnect_calls == 1
        assert device.connect_attempts >= 1

        terminal = [
            p for p in progress_events if p.state == FirmwareUpdateState.COMPLETE
        ]
        assert terminal, "expected a COMPLETE progress event"
        assert terminal[-1].percent_complete == pytest.approx(100.0)


# ---- update_firmware failure modes --------------------------------------


class TestUpdateFirmwareFailureModes:
    def test_when_bootloader_never_appears_throws_with_state_context(self) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        enumerator = _FakeHidEnumerator([])
        options = _fast_options()
        options.waiting_for_bootloader_timeout = 0.18
        options.poll_interval = 0.025

        service = DefaultFirmwareUpdateService(
            hid_transport=hid,
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=enumerator,
            options=options,
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        exc = exc_info.value
        assert exc.failed_state == FirmwareUpdateState.WAITING_FOR_BOOTLOADER
        assert service.current_state == FirmwareUpdateState.FAILED
        assert exc.recovery_guidance is not None

        cause = exc.__cause__
        assert isinstance(cause, TimeoutError)
        msg = str(cause)
        assert "No matching HID bootloader device was enumerated" in msg
        assert "VID=0x04D8" in msg
        assert "PID=0x003C" in msg

    def test_when_canceled_raises_and_transitions_to_failed(self) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        enumerator = _FakeHidEnumerator([])
        options = _fast_options()
        options.waiting_for_bootloader_timeout = 5.0

        service = DefaultFirmwareUpdateService(
            hid_transport=hid,
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=enumerator,
            options=options,
        )

        cancel_event = threading.Event()
        timer = threading.Timer(0.12, cancel_event.set)
        timer.daemon = True
        timer.start()
        try:
            hex_path = _make_temp_hex()
            try:
                with pytest.raises(FirmwareUpdateException) as exc_info:
                    service.update_firmware(
                        device, hex_path, cancel_event=cancel_event
                    )
            finally:
                os.remove(hex_path)
        finally:
            timer.cancel()

        assert service.current_state == FirmwareUpdateState.FAILED
        assert "canceled" in str(exc_info.value).lower()

    def test_when_program_ack_fails_retries_and_completes(self) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        hid.enqueue_read(bytes([0x01, 0x10]))  # version
        hid.enqueue_read(bytes([0x01, 0x02]))  # erase ack
        hid.enqueue_read(bytes([0x00]))         # invalid program ack
        hid.enqueue_read(bytes([0x01, 0x03]))  # retry program ack 1
        hid.enqueue_read(bytes([0x01, 0x03]))  # program ack 2
        hid.enqueue_read(bytes([0x01, 0x10]))  # verify version

        info = HidDeviceInfo(0x04D8, 0x003C, "path-1", "SN-1", "Bootloader")
        enumerator = _FakeHidEnumerator([[], [info]])
        protocol = _FakeBootloaderProtocol(
            [bytes([0xA1, 0x01]), bytes([0xA1, 0x02])]
        )
        options = _fast_options()
        options.flash_write_retry_count = 2
        options.flash_write_retry_delay = 0.005

        service = _build_pic32_service(hid, enumerator, protocol, options)

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert service.current_state == FirmwareUpdateState.COMPLETE
        assert len(hid.writes) == 7  # version + erase + 3 programs + verify + jump
        program_writes = [w for w in hid.writes if w and w[0] == 0x33]
        assert len(program_writes) == 3

    def test_when_hid_enumeration_throws_exception_has_enumeration_context(
        self,
    ) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        # The pre-FORceBoot snapshot succeeds; the failure under test is in
        # the wait for the bootloader that follows.
        enumerator = _ThrowingHidEnumerator(
            RuntimeError("HID backend unavailable."), succeed_first=1
        )
        # Tight WaitingForBootloader timeout so the retry loop exits
        # quickly into the timeout path.
        options = _fast_options()
        options.waiting_for_bootloader_timeout = 0.18
        options.poll_interval = 0.025

        service = DefaultFirmwareUpdateService(
            hid_transport=hid,
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=enumerator,
            options=options,
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        exc = exc_info.value
        assert exc.failed_state == FirmwareUpdateState.WAITING_FOR_BOOTLOADER
        assert service.current_state == FirmwareUpdateState.FAILED

        # The transient enumeration error is retried until the deadline
        # elapses; the timeout message surfaces it via
        # `Last HID enumeration error:`.
        cause = exc.__cause__
        assert isinstance(cause, TimeoutError)
        msg = str(cause)
        assert "Last HID enumeration error" in msg
        assert "HID backend unavailable" in msg
        assert "VID=0x04D8" in msg
        assert "PID=0x003C" in msg

    def test_when_multiple_bootloader_devices_enumerated_refuses_to_pick(
        self,
    ) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        info_a = HidDeviceInfo(0x04D8, 0x003C, "path-a", "SN-A", "Bootloader")
        info_b = HidDeviceInfo(0x04D8, 0x003C, "path-b", "SN-B", "Bootloader")
        enumerator = _FakeHidEnumerator([[], [info_a, info_b]])

        service = _build_pic32_service(
            hid,
            enumerator,
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        exc = exc_info.value
        assert exc.failed_state == FirmwareUpdateState.WAITING_FOR_BOOTLOADER
        assert isinstance(exc.__cause__, RuntimeError)
        assert "Multiple HID bootloader devices detected" in str(exc.__cause__)
        assert "SN-A" in str(exc.__cause__)
        assert "SN-B" in str(exc.__cause__)

    def test_bootloader_already_enumerated_before_force_boot_is_never_selected(
        self,
    ) -> None:
        # Audit finding: board B was already in its bootloader, and ours had
        # not re-enumerated by the first poll. B was the only candidate, so
        # the service connected to it and erased it.
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        other = HidDeviceInfo(0x04D8, 0x003C, "path-b", "SN-B", "Bootloader")
        enumerator = _FakeHidEnumerator([[other]] * 200)
        options = _fast_options()
        options.waiting_for_bootloader_timeout = 0.2

        service = _build_pic32_service(
            hid, enumerator, _FakeBootloaderProtocol([bytes([0xA1])]), options
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert exc_info.value.failed_state == FirmwareUpdateState.WAITING_FOR_BOOTLOADER
        assert "already enumerated before FORceBoot" in str(exc_info.value.__cause__)
        assert hid._connect_calls == 0
        assert hid.writes == []

    def test_bootloader_that_appears_after_force_boot_is_selected(self) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        connected_serials: List[Optional[str]] = []

        class _RecordingHid(_FakeHidTransport):
            def connect(self, vendor_id, product_id, serial_number=None, device_path=None):
                connected_serials.append(serial_number)
                super().connect(vendor_id, product_id, serial_number, device_path)

        hid = _RecordingHid()
        for reply in ([0x01, 0x10], [0x01, 0x02], [0x01, 0x03], [0x01, 0x10]):
            hid.enqueue_read(bytes(reply))
        other = HidDeviceInfo(0x04D8, 0x003C, "path-b", "SN-B", "Bootloader")
        ours = HidDeviceInfo(0x04D8, 0x003C, "path-a", "SN-A", "Bootloader")
        enumerator = _FakeHidEnumerator([[other], [other], [other, ours]])

        service = _build_pic32_service(
            hid, enumerator, _FakeBootloaderProtocol([bytes([0xA1])])
        )

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert service.current_state == FirmwareUpdateState.COMPLETE
        assert connected_serials == ["SN-A"]
        assert hid.connected_path == "path-a"

    def test_new_bootloader_on_another_usb_port_is_never_selected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Another board entering its bootloader during the wait is "new" to
        # the snapshot; where USB locations are known it is refused.
        import daqifi.firmware.update_service as update_service

        monkeypatch.setattr(update_service, "serial_port_location", lambda port: "1-2")
        monkeypatch.setattr(update_service, "_list_serial_port_names", lambda: frozenset())
        monkeypatch.setattr(
            update_service,
            "hid_device_location",
            lambda path: {"path-b": "1-4", "path-a": "1-2"}.get(path),
        )
        device = _FakeStreamingDevice("fake-port-3")
        device.transport = type("T", (), {"port_name": "/dev/ttyACM0"})()
        hid = _FakeHidTransport()
        other = HidDeviceInfo(0x04D8, 0x003C, "path-b", None, "Bootloader")
        options = _fast_options()
        options.waiting_for_bootloader_timeout = 0.2
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[]] + [[other]] * 200),
            _FakeBootloaderProtocol([bytes([0xA1])]),
            options,
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert "not shown to be on this device's USB port (1-2)" in str(
            exc_info.value.__cause__
        )
        assert hid._connect_calls == 0
        assert hid.writes == []

    def test_new_bootloader_with_unknown_location_is_not_selected_when_ours_is_known(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Audit finding (round 3): a board attached over usbip has no
        # readable location; "unknown" was accepted as a match.
        import daqifi.firmware.update_service as update_service

        monkeypatch.setattr(update_service, "serial_port_location", lambda port: "1-2")
        monkeypatch.setattr(update_service, "_list_serial_port_names", lambda: frozenset())
        monkeypatch.setattr(update_service, "hid_device_location", lambda path: None)
        device = _FakeStreamingDevice("fake-port-3")
        device.transport = type("T", (), {"port_name": "/dev/ttyACM0"})()
        hid = _FakeHidTransport()
        usbip_board = HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw2", None, "Bootloader")
        options = _fast_options()
        options.waiting_for_bootloader_timeout = 0.2
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[]] + [[usbip_board]] * 200),
            _FakeBootloaderProtocol([bytes([0xA1])]),
            options,
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException):
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert hid._connect_calls == 0
        assert hid.writes == []

    def test_reused_hidraw_node_on_our_port_is_not_mistaken_for_the_old_board(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Audit finding (round 3): board B held /dev/hidraw0 at the snapshot,
        # then unplugged; ours re-enumerated on the freed node.
        import daqifi.firmware.update_service as update_service

        monkeypatch.setattr(update_service, "serial_port_location", lambda port: "1-2")
        monkeypatch.setattr(update_service, "_list_serial_port_names", lambda: frozenset())
        locations = iter(["1-4"])  # snapshot sees B's port; later polls see ours
        monkeypatch.setattr(
            update_service, "hid_device_location", lambda path: next(locations, "1-2")
        )
        device = _FakeStreamingDevice("fake-port-3")
        device.transport = type("T", (), {"port_name": "/dev/ttyACM0"})()
        hid = _FakeHidTransport()
        for reply in ([0x01, 0x10], [0x01, 0x02], [0x01, 0x03], [0x01, 0x10]):
            hid.enqueue_read(bytes(reply))
        node = HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw0", None, "Bootloader")
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[node], [], [node]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert service.current_state == FirmwareUpdateState.COMPLETE
        assert hid.connected_path == "/dev/hidraw0"

    def test_node_handed_to_another_board_before_open_is_refused_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Audit finding (round 4): ours was selected at /dev/hidraw0 on port
        # 1-2, vanished before the open, and board B on 1-4 took the node.
        # Opening by path alone would have erased B.
        import daqifi.firmware.update_service as update_service

        swapped = {"done": False}
        monkeypatch.setattr(update_service, "serial_port_location", lambda port: "1-2")
        monkeypatch.setattr(update_service, "_list_serial_port_names", lambda: frozenset())
        monkeypatch.setattr(
            update_service,
            "hid_device_location",
            lambda path: "1-4" if swapped["done"] else "1-2",
        )

        class _SwappingHid(_FakeHidTransport):
            def connect(self, vendor_id, product_id, serial_number=None, device_path=None):
                swapped["done"] = True
                super().connect(vendor_id, product_id, serial_number, device_path)

        device = _FakeStreamingDevice("fake-port-3")
        device.transport = type("T", (), {"port_name": "/dev/ttyACM0"})()
        hid = _SwappingHid()
        node = HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw0", None, "Bootloader")
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[], [node]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert exc_info.value.failed_state == FirmwareUpdateState.CONNECTING
        assert "between selection and open" in str(exc_info.value.__cause__)
        assert hid._connect_calls == 1
        assert hid.writes == []
        assert not hid.is_connected

    @pytest.mark.parametrize("later", ["1-4", None])
    def test_location_that_passed_the_filter_is_the_one_checked_after_open(
        self, monkeypatch: pytest.MonkeyPatch, later
    ) -> None:
        # Audit finding (round 5): selection re-read the location after the
        # filter. If the node changed hands in between, the new board's port
        # (or None, which skipped the check) became the expected one.
        import daqifi.firmware.update_service as update_service

        reads = iter(["1-2"])
        monkeypatch.setattr(update_service, "serial_port_location", lambda port: "1-2")
        monkeypatch.setattr(update_service, "_list_serial_port_names", lambda: frozenset())
        monkeypatch.setattr(
            update_service, "hid_device_location", lambda path: next(reads, later)
        )
        device = _FakeStreamingDevice("fake-port-3")
        device.transport = type("T", (), {"port_name": "/dev/ttyACM0"})()
        hid = _FakeHidTransport()
        node = HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw0", None, "Bootloader")
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[], [node]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert "between selection and open" in str(exc_info.value.__cause__)
        assert hid.writes == []

    def test_new_bootloader_on_the_devices_usb_port_is_selected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import daqifi.firmware.update_service as update_service

        monkeypatch.setattr(update_service, "serial_port_location", lambda port: "1-2")
        monkeypatch.setattr(update_service, "_list_serial_port_names", lambda: frozenset())
        monkeypatch.setattr(
            update_service,
            "hid_device_location",
            lambda path: {"path-b": "1-4", "path-a": "1-2"}.get(path),
        )
        device = _FakeStreamingDevice("fake-port-3")
        device.transport = type("T", (), {"port_name": "/dev/ttyACM0"})()
        hid = _FakeHidTransport()
        for reply in ([0x01, 0x10], [0x01, 0x02], [0x01, 0x03], [0x01, 0x10]):
            hid.enqueue_read(bytes(reply))
        other = HidDeviceInfo(0x04D8, 0x003C, "path-b", None, "Bootloader")
        ours = HidDeviceInfo(0x04D8, 0x003C, "path-a", None, "Bootloader")
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[], [other, ours]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert service.current_state == FirmwareUpdateState.COMPLETE
        assert hid.connected_path == "path-a"

    def test_device_that_reboots_into_its_application_stops_the_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A board without the bootloader treats FORceBoot as a reboot: its
        # port drops and returns, and its own bootloader never appears. A
        # bootloader that shows up afterwards is another board's, even when
        # it is the only new one.
        import daqifi.firmware.update_service as update_service

        listings = iter([frozenset(), frozenset({"/dev/ttyACM0"})])
        monkeypatch.setattr(
            update_service,
            "_list_serial_port_names",
            lambda: next(listings, frozenset({"/dev/ttyACM0"})),
        )
        monkeypatch.setattr(update_service, "serial_port_location", lambda port: None)
        device = _FakeStreamingDevice("fake-port-3")
        device.transport = type("T", (), {"port_name": "/dev/ttyACM0"})()
        hid = _FakeHidTransport()
        foreign = HidDeviceInfo(0x04D8, 0x003C, "path-x", None, "Bootloader")
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[], [], [foreign]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert exc_info.value.failed_state == FirmwareUpdateState.WAITING_FOR_BOOTLOADER
        assert "came back as the application" in str(exc_info.value.__cause__)
        assert hid._connect_calls == 0
        assert hid.writes == []

    def test_return_to_application_is_seen_through_a_by_id_alias(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Port listings name the node (/dev/ttyACM0); the device may have
        # been opened through a /dev/serial/by-id alias.
        import daqifi.firmware.update_service as update_service

        node = tmp_path / "ttyACM0"
        node.write_text("")
        alias = tmp_path / "usb-DAQiFi-if00"
        alias.symlink_to(node)
        listings = iter([frozenset(), frozenset({str(node)})])
        monkeypatch.setattr(
            update_service,
            "_list_serial_port_names",
            lambda: next(listings, frozenset({str(node)})),
        )
        monkeypatch.setattr(update_service, "serial_port_location", lambda port: None)
        device = _FakeStreamingDevice("fake-port-3")
        device.transport = type("T", (), {"port_name": str(alias)})()
        hid = _FakeHidTransport()
        foreign = HidDeviceInfo(0x04D8, 0x003C, "path-x", None, "Bootloader")
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[], [], [foreign]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert "came back as the application" in str(exc_info.value.__cause__)
        assert hid.writes == []

    def test_reused_tty_name_on_another_usb_port_is_not_a_return(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Audit finding (round 6): another CDC board took the freed
        # /dev/ttyACM0 while ours sat in its bootloader on port 1-2.
        import daqifi.firmware.update_service as update_service

        listings = iter([frozenset()])
        monkeypatch.setattr(
            update_service,
            "_list_serial_port_names",
            lambda: next(listings, frozenset({"/dev/ttyACM0"})),
        )
        tty_locations = iter(["1-2"])  # at FORceBoot: ours; afterwards: B's
        monkeypatch.setattr(
            update_service, "serial_port_location", lambda port: next(tty_locations, "1-4")
        )
        monkeypatch.setattr(update_service, "hid_device_location", lambda path: "1-2")
        device = _FakeStreamingDevice("fake-port-3")
        device.transport = type("T", (), {"port_name": "/dev/ttyACM0"})()
        hid = _FakeHidTransport()
        for reply in ([0x01, 0x10], [0x01, 0x02], [0x01, 0x03], [0x01, 0x10]):
            hid.enqueue_read(bytes(reply))
        ours = HidDeviceInfo(0x04D8, 0x003C, "/dev/hidraw0", None, "Bootloader")
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[], [], [ours]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert service.current_state == FirmwareUpdateState.COMPLETE

    def test_port_still_listed_before_the_reset_lands_is_not_a_return(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import daqifi.firmware.update_service as update_service

        listings = iter([frozenset({"/dev/ttyACM0"}), None, frozenset()])
        monkeypatch.setattr(
            update_service, "_list_serial_port_names", lambda: next(listings, frozenset())
        )
        monkeypatch.setattr(update_service, "serial_port_location", lambda port: None)
        device = _FakeStreamingDevice("fake-port-3")
        device.transport = type("T", (), {"port_name": "/dev/ttyACM0"})()
        hid = _FakeHidTransport()
        for reply in ([0x01, 0x10], [0x01, 0x02], [0x01, 0x03], [0x01, 0x10]):
            hid.enqueue_read(bytes(reply))
        ours = HidDeviceInfo(0x04D8, 0x003C, "path-a", None, "Bootloader")
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[], [], [], [ours]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert service.current_state == FirmwareUpdateState.COMPLETE

    def test_when_enumeration_fails_before_force_boot_nothing_is_sent(
        self,
    ) -> None:
        # Without the pre-FORceBoot snapshot a stray bootloader can't be told
        # apart from ours, so the update stops before touching the device.
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        enumerator = _ThrowingHidEnumerator(RuntimeError("HID backend unavailable."))
        options = _fast_options()
        options.preparing_device_timeout = 0.2

        service = DefaultFirmwareUpdateService(
            hid_transport=hid,
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=enumerator,
            options=options,
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert exc_info.value.failed_state == FirmwareUpdateState.PREPARING_DEVICE
        assert device.sent_commands == []
        assert hid.writes == []

    def test_image_without_data_records_is_refused_before_any_device_io(
        self,
    ) -> None:
        # Audit finding: with the real protocol, an EOF-only image passed the
        # byte-count check; the service erased flash and reported COMPLETE.
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        service = DefaultFirmwareUpdateService(
            hid_transport=hid,
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
            bootloader_protocol=Pic32BootloaderProtocol(),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        fd, hex_path = tempfile.mkstemp(suffix=".hex")
        with os.fdopen(fd, "w") as handle:
            handle.write(":00000001FF\n")
        try:
            with pytest.raises(ValueError, match="no data records"):
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert device.sent_commands == []
        assert hid.writes == []

    def test_rejects_empty_hex_file_path(self) -> None:
        service = _build_pic32_service(
            _FakeHidTransport(),
            _FakeHidEnumerator([]),
            _FakeBootloaderProtocol([bytes([0x10])]),
        )
        with pytest.raises(ValueError):
            service.update_firmware(_FakeStreamingDevice(), "")
        with pytest.raises(ValueError):
            service.update_firmware(_FakeStreamingDevice(), "   ")

    def test_rejects_missing_hex_file(self) -> None:
        service = _build_pic32_service(
            _FakeHidTransport(),
            _FakeHidEnumerator([]),
            _FakeBootloaderProtocol([bytes([0x10])]),
        )
        with pytest.raises(FileNotFoundError):
            service.update_firmware(
                _FakeStreamingDevice(), "/no/such/firmware.hex"
            )

    def test_rejects_empty_hex_records(self) -> None:
        service = _build_pic32_service(
            _FakeHidTransport(),
            _FakeHidEnumerator([]),
            _FakeBootloaderProtocol([]),
        )
        hex_path = _make_temp_hex()
        try:
            with pytest.raises(ValueError):
                service.update_firmware(_FakeStreamingDevice(), hex_path)
        finally:
            os.remove(hex_path)


# ---- update_wifi_module --------------------------------------------------


_FAKE_CMD_EXE = "/fake/bin/cmd.exe"


class TestSeveralBoardsAttached:
    # Operator ruling 2026-10-27 (4=C): with more than one DAQiFi board on
    # USB at the start, a PIC32 update refuses before sending anything; a
    # single board goes ahead.

    @staticmethod
    def _service(hid, bootloaders_before, monkeypatch, application_ports):
        # application_ports: one listing for every scan, or a list of
        # listings returned scan by scan.
        import daqifi.firmware.update_service as update_service

        scans = (
            iter(application_ports)
            if application_ports and isinstance(application_ports[0], list)
            or application_ports and application_ports[0] is None
            else None
        )

        def _list(vid, pid):
            assert (vid, pid) == (0x04D8, 0xF794)
            return next(scans) if scans is not None else application_ports

        monkeypatch.setattr(update_service, "_list_usb_serial_ports", _list)
        monkeypatch.setattr(update_service, "_list_serial_port_names", lambda: frozenset())
        ours = HidDeviceInfo(0x04D8, 0x003C, "path-a", None, "Bootloader")
        return _build_pic32_service(
            hid,
            _FakeHidEnumerator([bootloaders_before, bootloaders_before + [ours]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

    def _assert_refused_before_anything_was_sent(self, service, device, hid) -> str:
        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)
        assert exc_info.value.failed_state == FirmwareUpdateState.PREPARING_DEVICE
        assert device.sent_commands == []
        assert hid._connect_calls == 0
        return str(exc_info.value.__cause__)

    def test_two_boards_in_their_application_are_refused(self, monkeypatch) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        service = self._service(hid, [], monkeypatch, ["TEST-PORT-1", "TEST-PORT-2"])

        message = self._assert_refused_before_anything_was_sent(service, device, hid)
        assert "2 DAQiFi boards" in message
        assert "TEST-PORT-1" in message and "TEST-PORT-2" in message
        assert "Disconnect every board except the one being updated" in message

    def test_a_board_left_in_bootloader_mode_counts(self, monkeypatch) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        stale = HidDeviceInfo(0x04D8, 0x003C, "path-x", None, "Bootloader")
        service = self._service(hid, [stale], monkeypatch, ["TEST-PORT-1"])

        message = self._assert_refused_before_anything_was_sent(service, device, hid)
        assert "1 in bootloader mode" in message

    def test_a_single_board_goes_ahead(self, monkeypatch) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        for reply in ([0x01, 0x10], [0x01, 0x02], [0x01, 0x03], [0x01, 0x10]):
            hid.enqueue_read(bytes(reply))
        service = self._service(hid, [], monkeypatch, ["TEST-PORT-1"])

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)
        assert service.current_state == FirmwareUpdateState.COMPLETE
        assert hid.connected_path == "path-a"

    def test_an_unlistable_serial_bus_is_refused(self, monkeypatch) -> None:
        # Audit finding on #168 (round 1): a failed listing counted as no
        # application boards, so two attached boards passed the check.
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        service = self._service(hid, [], monkeypatch, [None, None, None])

        message = self._assert_refused_before_anything_was_sent(service, device, hid)
        assert "Could not list serial ports" in message

    def test_a_listing_that_fails_once_is_retried(self, monkeypatch) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        for reply in ([0x01, 0x10], [0x01, 0x02], [0x01, 0x03], [0x01, 0x10]):
            hid.enqueue_read(bytes(reply))
        service = self._service(
            hid, [], monkeypatch, [None, ["TEST-PORT-1"], ["TEST-PORT-1"]]
        )

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)
        assert service.current_state == FirmwareUpdateState.COMPLETE

    def test_a_board_entering_its_bootloader_during_the_check_is_counted(
        self, monkeypatch
    ) -> None:
        # Audit finding on #168 (round 1): B left its application after the
        # HID snapshot and before the serial scan, so it was in neither.
        import daqifi.firmware.update_service as update_service

        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        service = self._service(hid, [], monkeypatch, [])
        enumerator = service._hid_device_enumerator
        batches_before = len(enumerator._batches)
        # B's serial port is there until the HID snapshot has been taken,
        # and gone afterwards (B is rebooting into its bootloader).
        monkeypatch.setattr(
            update_service, "_list_usb_serial_ports",
            lambda vid, pid: ["TEST-PORT-1", "TEST-PORT-2"]
            if len(enumerator._batches) == batches_before else ["TEST-PORT-1"],
        )

        message = self._assert_refused_before_anything_was_sent(service, device, hid)
        assert "TEST-PORT-2" in message

    @pytest.mark.parametrize("bad", [0x10000, -1, 0.1, True])
    def test_application_usb_ids_are_validated(self, bad) -> None:
        with pytest.raises(ValueError, match="application_product_id"):
            FirmwareUpdateServiceOptions(application_product_id=bad).validate()

    def test_ports_without_usb_ids_count_when_they_are_cdc_acm(self, monkeypatch) -> None:
        # Audit finding on #168 (round 4): with /sys/class/tty hidden, pyserial
        # lists the boards' ttyACM nodes with vid=None, the VID/PID filter
        # dropped both, and two boards passed as none.
        from types import SimpleNamespace

        from serial.tools import list_ports

        listing = [
            SimpleNamespace(device="/dev/ttyACM0", vid=None, pid=None),
            SimpleNamespace(device="/dev/ttyACM1", vid=None, pid=None),
            SimpleNamespace(device="/dev/cu.usbmodem1101", vid=None, pid=None),
            SimpleNamespace(device="/dev/ttyS0", vid=None, pid=None),
            SimpleNamespace(device="COM1", vid=None, pid=None),
            SimpleNamespace(device="TEST-PORT-7", vid=0x04D8, pid=0xF794),
            SimpleNamespace(device="TEST-PORT-8", vid=0x0403, pid=0x6001),
        ]
        monkeypatch.setattr(list_ports, "comports", lambda: listing)

        assert _real_list_usb_serial_ports(0x04D8, 0xF794) == [
            "/dev/cu.usbmodem1101", "/dev/ttyACM0", "/dev/ttyACM1", "TEST-PORT-7",
        ]

    def test_positional_options_keep_their_meaning(self) -> None:
        # Audit finding on #168 (round 2): the new fields were inserted
        # after the bootloader IDs, so FirmwareUpdateServiceOptions(0x04D8,
        # 0x003C, 0.1) set application_vendor_id=0.1 instead of poll_interval.
        options = FirmwareUpdateServiceOptions(0x04D8, 0x003C, 0.1, 20.0, 45.0)
        assert options.poll_interval == 0.1
        assert options.preparing_device_timeout == 20.0
        assert options.waiting_for_bootloader_timeout == 45.0
        assert (options.application_vendor_id, options.application_product_id) == (0x04D8, 0xF794)


class TestUpdateWifiModule:
    @pytest.fixture(autouse=True)
    def _pin_cmd_exe_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The default tool is winc_flash_tool.cmd, which the service wraps in
        # cmd.exe found via shutil.which. The process runner here is a fake,
        # so nothing is executed -- but without this pin the lookup reads the
        # host PATH, and these tests passed only where cmd.exe happened to be
        # on it (Windows, WSL with interop) and failed on a plain Linux CI
        # runner. Pinned, every host takes the same branch.
        real_which = shutil.which

        def _which(name, *args, **kwargs):
            if name == "cmd.exe":
                return _FAKE_CMD_EXE
            return real_which(name, *args, **kwargs)

        monkeypatch.setattr(shutil, "which", _which)

    def test_cmd_tool_is_wrapped_in_cmd_exe(self) -> None:
        device = _FakeStreamingDevice("fake-port-6")
        runner = _FakeExternalProcessRunner()
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert runner.last_request is not None
        assert runner.last_request.file_name == _FAKE_CMD_EXE
        assert runner.last_request.arguments[:3] == ["/d", "/v:off", "/c"]
        assert "winc_flash_tool.cmd" in runner.last_request.arguments[3]

    def test_when_cmd_exe_missing_throws_file_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
        device = _FakeStreamingDevice("fake-port-6")
        runner = _FakeExternalProcessRunner()
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert isinstance(exc_info.value.__cause__, FileNotFoundError)
        assert "cmd.exe" in str(exc_info.value.__cause__)
        assert runner.last_request is None
        # Resolved before the device is switched into WiFi-update mode.
        assert device.sent_commands == []
        assert device.disconnect_calls == 0

    def test_port_is_the_serial_transports_port_not_the_device_name(self) -> None:
        # Audit finding: the factory names every device "DaqifiDevice", and
        # that name was handed to the flash tool as its port.
        device = _FakeStreamingDevice("DaqifiDevice")
        device.transport = type("T", (), {"port_name": "TEST-PORT-7"})()
        runner = _FakeExternalProcessRunner()
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        arguments = runner.last_request.arguments
        flat = arguments if isinstance(arguments, str) else " ".join(arguments)
        assert "TEST-PORT-7" in flat
        assert "DaqifiDevice" not in flat

    def test_device_without_a_serial_port_fails_before_update_mode(self) -> None:
        device = _FakeStreamingDevice("DaqifiDevice")
        device.transport = type("TcpLike", (), {})()
        runner = _FakeExternalProcessRunner()
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert "wifi_port_override" in str(exc_info.value.__cause__)
        assert device.sent_commands == []
        assert runner.last_request is None

    @pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
    def test_dotdot_through_a_symlink_keeps_the_os_meaning(self, tmp_path) -> None:
        # Audit finding on #166 (round 7): os.path.abspath collapsed
        # "link/../bundle" textually, naming a different bundle than the OS.
        from daqifi.firmware.update_service import _anchor_to_cwd

        real = tmp_path / "releases" / "current"
        real.mkdir(parents=True)
        link = tmp_path / "srv" / "selected"
        link.parent.mkdir()
        link.symlink_to(real)
        dotted = os.path.join(str(link), "..", "bundle")
        assert _anchor_to_cwd(dotted) == dotted
        assert os.path.realpath(_anchor_to_cwd(dotted)) == str(tmp_path / "releases" / "bundle")

    # ---- Argument matrix (audit rounds 15-18 on #166) ----------------------
    # host x tool type -> template parser -> how the result is passed:
    #   native Windows, .cmd/.bat  cmd rules      one "/d /v:off /s /c" string
    #   native Windows, other      C-runtime      list (list2cmdline)
    #   WSL,            .cmd/.bat  cmd rules      list, WSL quotes each argument
    #   WSL,            .exe/.com  C-runtime      list, paths via wslpath -w
    #   POSIX,          other      shlex          list (argv)
    #   POSIX without cmd.exe, .cmd               refused before update mode
    # Each cell runs through update_wifi_module, i.e. the request builder.

    @staticmethod
    def _matrix_request(monkeypatch, tmp_path, *, native, tool, template, fw_name="fw dir"):
        import daqifi.firmware.update_service as update_service

        monkeypatch.setattr(update_service, "_is_native_windows", lambda: native)
        monkeypatch.setattr(
            update_service, "_path_for_windows_tool",
            lambda path: path if native or not path.startswith("/")
            else "C:\\WSL" + path.replace("/", "\\"),
        )
        device = _FakeStreamingDevice("fake-port-6")
        runner = _FakeExternalProcessRunner()
        options = _fast_options()
        options.wifi_flash_tool_file_name = tool
        options.wifi_flash_tool_arguments_template = template
        options.wifi_port_override = "TESTPORT"
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=options,
        )
        firmware_dir = tmp_path / fw_name
        firmware_dir.mkdir()
        (firmware_dir / tool).write_text("")
        return service, device, runner, str(firmware_dir)

    def test_matrix_native_windows_cmd_is_one_verbatim_line(self, monkeypatch, tmp_path) -> None:
        service, device, runner, fw = self._matrix_request(
            monkeypatch, tmp_path, native=True, tool="winc_flash_tool.cmd",
            template='/p {port} /i "{firmwarePath}" /w',
        )
        service.update_wifi_module(device, fw)
        assert runner.last_request.arguments == (
            f'/d /v:off /s /c ""winc_flash_tool.cmd" /p "TESTPORT" /i "{fw}" /w"'
        )

    @pytest.mark.parametrize("template,expected", [
        # Round 18: cmd rules kept the backslashes and mangled the escaped quotes.
        (r'-c "print(\"flash complete\")" /p {port}', ["-c", 'print("flash complete")', "/p", "TESTPORT"]),
        (r'/i "{firmwarePath}" /w', ["/i", "<fw>", "/w"]),
        (r"/i C:\O'Brien\image.bin", ["/i", r"C:\O'Brien\image.bin"]),
    ])
    def test_matrix_native_windows_exe_uses_c_runtime_rules(
        self, monkeypatch, tmp_path, template, expected
    ) -> None:
        service, device, runner, fw = self._matrix_request(
            monkeypatch, tmp_path, native=True, tool="flash.exe", template=template,
        )
        service.update_wifi_module(device, fw)
        assert runner.last_request.arguments == [fw if a == "<fw>" else a for a in expected]

    def test_matrix_native_windows_cmd_consumes_carets_outside_quotes(self, monkeypatch, tmp_path) -> None:
        # Audit finding on #166 (round 19): "^" escapes were kept and then
        # quoted, so the script got "^&". Checked on real cmd.exe: with this
        # line %~2 is A&B, %~4 is C:\x^y (literal inside quotes), %~6 is A^B.
        service, device, runner, fw = self._matrix_request(
            monkeypatch, tmp_path, native=True, tool="winc_flash_tool.cmd",
            template=r'/n A^&B /m "C:\x^y" /k A^^B /p {port}',
        )
        service.update_wifi_module(device, fw)
        assert runner.last_request.arguments == (
            r'/d /v:off /s /c ""winc_flash_tool.cmd" /n "A&B" /m "C:\x^y" /k "A^B" /p "TESTPORT""'
        )

    def test_matrix_wsl_cmd_consumes_carets_and_refuses_what_they_escaped(self, monkeypatch, tmp_path) -> None:
        # Under WSL the arguments reach cmd.exe bare, so an escaped "&" would
        # run as syntax again: refused before update mode, like a literal one.
        service, device, runner, fw = self._matrix_request(
            monkeypatch, tmp_path, native=False, tool="winc_flash_tool.cmd",
            template=r"/n A^&B /p {port}", fw_name="fw",
        )
        with pytest.raises(FirmwareUpdateException) as exc_info:
            service.update_wifi_module(device, fw)
        assert "'A&B'" in str(exc_info.value.__cause__)
        assert device.sent_commands == [] and runner.last_request is None

    def test_matrix_wsl_cmd_is_a_list_split_by_cmd_rules(self, monkeypatch, tmp_path) -> None:
        service, device, runner, fw = self._matrix_request(
            monkeypatch, tmp_path, native=False, tool="winc_flash_tool.cmd",
            template=r"/p {port} /i C:\O'Brien\image.bin /k {firmwarePath}", fw_name="fw",
        )
        service.update_wifi_module(device, fw)
        assert runner.last_request.arguments == [
            "/d", "/v:off", "/c", "winc_flash_tool.cmd", "/p", "TESTPORT",
            "/i", r"C:\O'Brien\image.bin", "/k", "C:\\WSL" + fw.replace("/", "\\"),
        ]

    def test_matrix_wsl_exe_uses_c_runtime_rules_and_windows_paths(self, monkeypatch, tmp_path) -> None:
        # A Windows program started from WSL parses its line with the C
        # runtime; shlex would have eaten C:\fw's backslashes.
        service, device, runner, fw = self._matrix_request(
            monkeypatch, tmp_path, native=False, tool="flash.exe",
            template=r'-c "print(\"ok\")" /i "{firmwarePath}" /x C:\fw\image.bin',
        )
        service.update_wifi_module(device, fw)
        assert runner.last_request.arguments == [
            "-c", 'print("ok")', "/i", "C:\\WSL" + fw.replace("/", "\\"), "/x", r"C:\fw\image.bin",
        ]

    def test_matrix_posix_program_uses_shlex(self, monkeypatch, tmp_path) -> None:
        service, device, runner, fw = self._matrix_request(
            monkeypatch, tmp_path, native=False, tool="flash-tool",
            template='--bundle "{firmwarePath}" --name "O\'Brien" --port={port}', fw_name="O'Brien fw",
        )
        service.update_wifi_module(device, fw)
        assert runner.last_request.arguments == [
            "--bundle", fw, "--name", "O'Brien", "--port=TESTPORT",
        ]

    def test_matrix_cmd_without_cmd_exe_is_refused_before_update_mode(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
        service, device, runner, fw = self._matrix_request(
            monkeypatch, tmp_path, native=False, tool="winc_flash_tool.cmd", template="/p {port}",
        )
        with pytest.raises(FirmwareUpdateException) as exc_info:
            service.update_wifi_module(device, fw)
        assert "cmd.exe was not found" in str(exc_info.value.__cause__)
        assert device.sent_commands == [] and runner.last_request is None

    @pytest.mark.parametrize("native,tool,template", [
        (False, "winc_flash_tool.cmd", '/p "{port}'),
        (True, "winc_flash_tool.cmd", '/p "{port}'),
        (False, "flash-tool", "--bundle '{firmwarePath}"),
    ])
    def test_matrix_unsplittable_templates_are_refused_before_update_mode(
        self, monkeypatch, tmp_path, native, tool, template
    ) -> None:
        # The C-runtime rules have no unsplittable input (an open quote runs
        # to the end); cmd and shlex templates with an open quote are refused.
        service, device, runner, fw = self._matrix_request(
            monkeypatch, tmp_path, native=native, tool=tool, template=template,
        )
        with pytest.raises(FirmwareUpdateException) as exc_info:
            service.update_wifi_module(device, fw)
        assert "can't be split" in str(exc_info.value.__cause__)
        assert device.sent_commands == [] and runner.last_request is None

    @staticmethod
    def _native_tool_request(firmware_dir: str, template: str):
        device = _FakeStreamingDevice("fake-port-6")
        runner = _FakeExternalProcessRunner()
        options = _fast_options()
        options.wifi_flash_tool_file_name = "flash-tool"
        options.wifi_flash_tool_arguments_template = template
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=options,
        )
        os.makedirs(firmware_dir, exist_ok=True)
        with open(os.path.join(firmware_dir, "flash-tool"), "w") as handle:
            handle.write("#!/bin/sh\n")
        return service, device, runner

    @pytest.mark.skipif(os.name != "posix", reason="POSIX template split")
    @pytest.mark.parametrize("template", ["--bundle {firmwarePath} /w", '--bundle "{firmwarePath}" /w'])
    @pytest.mark.parametrize("name", ["O'Brien", "a b", 'q"x', "plain"])
    def test_each_value_reaches_a_native_tool_as_one_argument(self, tmp_path, template, name) -> None:
        # Audit findings on #166 (rounds 9 and 16): a quoted value was parsed
        # again by the runner's split, and inside the template's own quotes
        # an apostrophe raised "No closing quotation" after the device had
        # entered update mode. The template is split first now.
        firmware_dir = str(tmp_path / name)
        service, device, runner = self._native_tool_request(firmware_dir, template)

        service.update_wifi_module(device, firmware_dir)

        assert runner.last_request.arguments == ["--bundle", firmware_dir, "/w"]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX template split")
    def test_unbalanced_template_fails_before_update_mode(self, tmp_path) -> None:
        firmware_dir = str(tmp_path / "fw")
        service, device, runner = self._native_tool_request(firmware_dir, '--bundle "{firmwarePath} /w')

        with pytest.raises(FirmwareUpdateException) as exc_info:
            service.update_wifi_module(device, firmware_dir)

        assert "can't be split" in str(exc_info.value.__cause__)
        assert device.sent_commands == []
        assert runner.last_request is None

    def test_a_value_containing_a_placeholder_is_not_substituted_again(self) -> None:
        from daqifi.firmware.update_service import _substitute_placeholders

        assert _substitute_placeholders(
            ["/p", "{port}", "/i", "{firmwarePath}"], "{firmwarePath}", r"C:\fw"
        ) == ["/p", "{firmwarePath}", "/i", r"C:\fw"]

    def test_relative_firmware_path_is_made_absolute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Audit finding: the tool runs with its own directory as cwd, so a
        # relative tool path was looked up as bundle/bundle/<tool>.
        device = _FakeStreamingDevice("fake-port-6")
        runner = _FakeExternalProcessRunner()
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        firmware_dir = _make_temp_wifi_dir()
        parent, name = os.path.split(firmware_dir)
        monkeypatch.chdir(parent)
        try:
            service.update_wifi_module(device, name)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        # The .cmd script is named relative to the working directory, which
        # is absolute (cmd.exe under WSL can't open a Linux path argument).
        arguments = runner.last_request.arguments
        flat = arguments if isinstance(arguments, str) else " ".join(arguments)
        assert "winc_flash_tool.cmd" in flat
        assert firmware_dir not in flat
        assert runner.last_request.working_directory == firmware_dir

    def test_relative_path_to_a_native_tool_is_absolute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = _FakeStreamingDevice("fake-port-6")
        runner = _FakeExternalProcessRunner()
        options = _fast_options()
        options.wifi_flash_tool_file_name = "flash-tool"
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=options,
        )

        firmware_dir = _make_temp_directory()
        with open(os.path.join(firmware_dir, "flash-tool"), "w") as handle:
            handle.write("#!/bin/sh\n")
        parent, name = os.path.split(firmware_dir)
        monkeypatch.chdir(parent)
        try:
            service.update_wifi_module(device, name)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert runner.last_request.file_name == os.path.join(firmware_dir, "flash-tool")

    def test_firmware_path_given_to_cmd_exe_is_translated_under_wsl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Audit finding on #166 (round 10): WSL passes arguments to Windows
        # programs untranslated, so cmd.exe got a Linux path it can't open.
        import subprocess as sp

        import daqifi.firmware.update_service as update_service

        monkeypatch.setattr(update_service, "_running_under_wsl", lambda: True)
        real_run = sp.run

        def _wslpath(cmd, *a, **k):
            if cmd[:2] == ["wslpath", "-w"]:
                return sp.CompletedProcess(cmd, 0, stdout="C:\\fw\\bundle\n", stderr="")
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(update_service.subprocess, "run", _wslpath)
        device = _FakeStreamingDevice("fake-port-6")
        runner = _FakeExternalProcessRunner()
        options = _fast_options()
        options.wifi_flash_tool_arguments_template = "/p {port} /i {firmwarePath}"
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=options,
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        arguments = runner.last_request.arguments
        flat = arguments if isinstance(arguments, str) else " ".join(arguments)
        assert "C:\\fw\\bundle" in flat
        assert firmware_dir not in flat

    def test_cmd_exe_line_on_native_windows_is_one_verbatim_string(self) -> None:
        # Verified on a real Windows interpreter: main's ["/c", command]
        # went through list2cmdline, which escaped the inner quotes as \",
        # and cmd.exe reported the script as "not recognized".
        from daqifi.firmware.update_service import _cmd_exe_arguments

        arguments = _cmd_exe_arguments(
            "winc_flash_tool.cmd", "/p {port} /i {firmwarePath}", "A&B", r"C:\fw dir",
            native_windows=True,
        )
        assert arguments == r'/d /v:off /s /c ""winc_flash_tool.cmd" /p "A&B" /i "C:\fw dir""'

    @pytest.mark.parametrize("bad", [r"C:\fw&calc.exe&rem", r"C:\fw|x", r"C:\%PATH%", "PORT^1"])
    def test_cmd_metacharacters_are_refused_under_wsl(self, bad) -> None:
        # Audit finding on #166 (round 11): WSL leaves an argument without
        # whitespace unquoted, so "&" in a path ran what followed as a command.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        with pytest.raises(ValueError, match="cmd.exe"):
            _cmd_exe_arguments(
                "winc_flash_tool.cmd", "/p {port} /i {firmwarePath}",
                bad if bad.startswith("PORT") else "PORT1",
                bad if not bad.startswith("PORT") else r"C:\fw",
                native_windows=False,
            )

    def test_program_files_x86_is_allowed_under_wsl(self) -> None:
        from daqifi.firmware.update_service import _cmd_exe_arguments

        assert _cmd_exe_arguments(
            "t.cmd", "/i {firmwarePath}", "P", r"C:\Program Files (x86)\fw",
            native_windows=False,
        )[-1] == r"C:\Program Files (x86)\fw"

    def test_bundle_on_the_linux_filesystem_is_refused_under_wsl_before_update_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Audit finding on #166 (round 11): cmd.exe rejects the UNC working
        # directory WSL gives a Linux-filesystem bundle, so the script was
        # not found after the device had entered update mode.
        import daqifi.firmware.update_service as update_service

        if os.name == "nt":
            pytest.skip("WSL-only path")
        monkeypatch.setattr(
            update_service,
            "_path_for_windows_tool",
            lambda path: "\\\\wsl.localhost\\Ubuntu" + path.replace("/", "\\"),
        )
        device = _FakeStreamingDevice("fake-port-6")
        runner = _FakeExternalProcessRunner()
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert "Windows drive" in str(exc_info.value.__cause__)
        assert device.sent_commands == []
        assert runner.last_request is None

    @pytest.mark.parametrize("native", [True, False])
    def test_percent_and_double_quote_are_refused_on_every_host(self, native) -> None:
        # Audit finding on #166 (round 12): cmd.exe expands %VAR% even inside
        # quotes, so a literal %USERNAME% in a path reached the tool changed.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        for bad in (r"C:\fw\%USERNAME%", 'C:\\fw"x'):
            with pytest.raises(ValueError, match="cmd.exe"):
                _cmd_exe_arguments(
                    "winc_flash_tool.cmd", "/p {port} /i {firmwarePath}", "P", bad,
                    native_windows=native,
                )

    def test_ampersand_stays_allowed_and_quoted_on_native_windows(self) -> None:
        from daqifi.firmware.update_service import _cmd_exe_arguments

        assert '"A&B"' in _cmd_exe_arguments(
            "t.cmd", "/p {port}", "A&B", r"C:\fw", native_windows=True
        )

    def test_literal_windows_paths_in_the_template_keep_backslashes_under_wsl(self) -> None:
        # Audit finding on #166 (round 12): POSIX shlex turned
        # C:\firmware\image.bin into C:firmwareimage.bin.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        arguments = _cmd_exe_arguments(
            "t.cmd", r"/p {port} /i C:\firmware\image.bin", "P1", r"C:\fw",
            native_windows=False,
        )
        assert arguments[-1] == r"C:\firmware\image.bin"

    @pytest.mark.parametrize("template,expected", [
        (r'/p {port} /i "C:\Firmware Files\image.bin" /w', ["/p", "P1", "/i", r"C:\Firmware Files\image.bin", "/w"]),
        (r"/p {port} /i C:\firmware\image.bin", ["/p", "P1", "/i", r"C:\firmware\image.bin"]),
        (r"/p {port} /n O'Brien", ["/p", "P1", "/n", "O'Brien"]),
    ])
    def test_template_is_split_like_a_windows_command_line_under_wsl(self, template, expected) -> None:
        # Audit finding on #166 (round 13): str.split kept a quoted literal's
        # quotes and broke it in two; WSL then escaped the quotes.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        assert _cmd_exe_arguments("t.cmd", template, "P1", r"C:\fw", native_windows=False) == ["/d", "/v:off", "/c", "t.cmd"] + expected

    @pytest.mark.parametrize("native", [True, False])
    def test_script_name_is_checked_like_the_other_values(self, native) -> None:
        # Audit finding on #166 (round 14): under WSL "flash&calc.exe&rem.cmd"
        # ran calc.exe; the script name wasn't validated.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        bad = "flash%TEMP%.cmd" if native else "flash&calc.exe&rem.cmd"
        with pytest.raises(ValueError, match="tool name"):
            _cmd_exe_arguments(bad, "/p {port}", "P1", r"C:\fw", native_windows=native)

    def test_hash_in_a_template_path_is_not_a_comment(self) -> None:
        # Audit finding on #166 (round 14): shlex's default "#" comment
        # truncated C:\fw#1\image.bin and dropped the rest of the template.
        from daqifi.firmware.update_service import _split_windows_template

        assert _split_windows_template(r"/p {port} /i C:\fw#1\image.bin /w") == [
            "/p", "{port}", "/i", r"C:\fw#1\image.bin", "/w",
        ]

    def test_script_name_with_whitespace_is_refused_under_wsl(self) -> None:
        # Audit finding on #166 (round 12): WSL quotes it first, and cmd.exe
        # /c then strips the leading quote.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        with pytest.raises(ValueError, match="whitespace"):
            _cmd_exe_arguments("flash tool.cmd", "/p {port}", "P1", r"C:\fw", native_windows=False)
        assert _cmd_exe_arguments(
            "flash tool.cmd", "/p {port}", "P1", r"C:\fw", native_windows=True
        ).startswith('/d /v:off /s /c ""flash tool.cmd"')

    def test_unc_bundle_is_refused_on_native_windows_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Audit finding on #166 (round 12): \\server\share is a UNC working
        # directory cmd.exe refuses on any host.
        import daqifi.firmware.update_service as update_service

        monkeypatch.setattr(
            update_service, "_path_for_windows_tool", lambda path: r"\\server\share\bundle"
        )
        device = _FakeStreamingDevice("fake-port-6")
        runner = _FakeExternalProcessRunner()
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )
        firmware_dir = _make_temp_wifi_dir()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)
        assert "local Windows drive" in str(exc_info.value.__cause__)
        assert device.sent_commands == []

    def test_cmd_exe_arguments_under_wsl_are_separate_and_unquoted(self) -> None:
        # Verified under real WSL interop: quotes inside an argument reach
        # cmd.exe as \", so WSL must do the quoting.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        arguments = _cmd_exe_arguments(
            "winc_flash_tool.cmd", "/p {port} /i {firmwarePath}", "PORT 1", r"C:\fw dir",
            native_windows=False,
        )
        assert arguments == ["/d", "/v:off", "/c", "winc_flash_tool.cmd", "/p", "PORT 1", "/i", r"C:\fw dir"]

    @pytest.mark.parametrize("template", [
        r'/p {port} /i "C:\fw&calc.exe&rem\image.bin" /w',
        r"/p {port} /i C:\fw|x /w",
        r"/p {port} /k %TEMP%",
    ])
    def test_template_literals_are_checked_for_metacharacters_under_wsl(self, template) -> None:
        # Audit finding on #166 (round 15): the split removed a quoted
        # literal's quotes and WSL left it bare, so "&" in the template ran
        # calc.exe; only the substituted values were checked.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        with pytest.raises(ValueError, match="template"):
            _cmd_exe_arguments("t.cmd", template, "P1", r"C:\fw", native_windows=False)

    def test_percent_in_the_template_is_refused_on_native_windows(self) -> None:
        # Quoting doesn't stop %VAR% expansion in the template's own text.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        with pytest.raises(ValueError, match="template"):
            _cmd_exe_arguments(
                "t.cmd", r'/p {port} /i "C:\%USERNAME%\fw"', "P1", r"C:\fw",
                native_windows=True,
            )
        # A quoted "&" in the template stays quoted in the verbatim line.
        assert '"C:\\a&b"' in _cmd_exe_arguments(
            "t.cmd", r'/p {port} /i "C:\a&b"', "P1", r"C:\fw", native_windows=True
        )

    @pytest.mark.skipif(os.name == "nt", reason="the POSIX split is what failed")
    @pytest.mark.parametrize("template,tail", [
        (r"/p {port} /i C:\O'Brien\image.bin", ["/i", r"C:\O'Brien\image.bin"]),
        (r'/p {port} /i "C:\firmware\"', ["/i", "C:\\firmware\\"]),
    ])
    def test_a_cmd_template_is_split_by_windows_rules_through_the_request_builder(
        self, template, tail
    ) -> None:
        # Audit finding on #166 (round 17): the request builder POSIX-split
        # the template before looking at the tool type, so under WSL a legal
        # .cmd template with an apostrophe or a quoted trailing backslash
        # raised "No closing quotation" before reaching _cmd_exe_arguments.
        device = _FakeStreamingDevice("fake-port-6")
        runner = _FakeExternalProcessRunner()
        options = _fast_options()
        options.wifi_flash_tool_arguments_template = template
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=options,
        )
        firmware_dir = _make_temp_wifi_dir()
        try:
            service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        arguments = runner.last_request.arguments
        assert arguments[:4] == ["/d", "/v:off", "/c", "winc_flash_tool.cmd"]
        assert arguments[4] == "/p" and arguments[-2:] == tail

    def test_a_placeholder_the_template_quoted_is_quoted_once_on_native_windows(self) -> None:
        # Audit finding on #166 (round 16): '/i "{firmwarePath}"' became
        # ""C:\fw&calc.exe&rem"", the quote pairs cancelled and cmd.exe ran
        # calc.exe. Checked on real cmd.exe: the script gets the path intact.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        assert _cmd_exe_arguments(
            "t.cmd", '/p {port} /i "{firmwarePath}" /w', "P1", r"C:\fw&calc.exe&rem",
            native_windows=True,
        ) == r'/d /v:off /s /c ""t.cmd" /p "P1" /i "C:\fw&calc.exe&rem" /w"'

    def test_cmd_exe_disables_delayed_expansion_on_every_host(self) -> None:
        # Audit finding on #166 (round 15): with EnableDelayedExpansion set in
        # the registry, !BUILD! in a quoted path expanded. Verified on real
        # cmd.exe: /v:on /d /s /c passed C:\fw\other, /v:on /d /v:off /s /c
        # passed C:\fw\!BUILD!.
        from daqifi.firmware.update_service import _cmd_exe_arguments

        native = _cmd_exe_arguments(
            "t.cmd", "/i {firmwarePath}", "P1", r"C:\fw\!BUILD!", native_windows=True
        )
        assert native.startswith("/d /v:off /s /c ")
        assert r'"C:\fw\!BUILD!"' in native
        assert _cmd_exe_arguments(
            "t.cmd", "/i {firmwarePath}", "P1", r"C:\fw", native_windows=False
        )[:3] == ["/d", "/v:off", "/c"]

    @pytest.mark.parametrize("value", ["C:\\bundle\\", 'a"b', 'a\\"b', "C:\\a b\\x\\", ""])
    def test_native_windows_tool_arguments_go_as_a_list(self, value) -> None:
        # Audit finding on #166 (round 15): "C:\bundle\" ended in \", which a
        # C-runtime program reads as an escaped quote, swallowing the next
        # flag. On Windows the arguments are now a list, which the runner
        # joins with list2cmdline (the C-runtime rules); checked on real
        # Windows that sys.argv round-trips each value and the next "/w".
        from daqifi.firmware.update_service import (
            _split_windows_template,
            _substitute_placeholders,
        )

        assert _substitute_placeholders(
            _split_windows_template('/i "{firmwarePath}" /w'), "P1", value
        ) == ["/i", value, "/w"]

    def test_happy_path_sends_expected_commands_and_reports_progress(
        self,
    ) -> None:
        device = _FakeStreamingDevice("fake-port-7")
        runner = _FakeExternalProcessRunner(
            ExternalProcessResult(
                exit_code=0,
                timed_out=False,
                duration_seconds=0.01,
                standard_output_lines=(
                    "begin write operation",
                    "67%",
                    "begin verify operation",
                    "Operation completed successfully",
                ),
                standard_error_lines=tuple(),
            )
        )
        options = _fast_options()
        options.post_lan_firmware_mode_delay = 0.005
        options.post_wifi_reconnect_delay = 0.005

        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=options,
        )

        progress_events: List[FirmwareUpdateProgress] = []
        firmware_dir = _make_temp_wifi_dir()
        try:
            service.update_wifi_module(
                device, firmware_dir, progress=progress_events.append
            )
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert service.current_state == FirmwareUpdateState.COMPLETE
        # The bridge handling's queries aside.
        assert [
            c for c in device.sent_commands
            if c not in (
                "SYSTem:ERRor?", "*IDN?", "SYSTem:POWer:STATe?", "SYSTem:STReam:STOP"
            )
        ] == [
            "SYSTem:POWer:STATe 1",
            "SYSTem:COMMUnicate:LAN:FWUpdate",
            "SYSTem:COMMunicate:LAN:APPLY",
            "SYSTem:USB:SetTransparentMode 0",
            "SYSTem:COMMunicate:LAN:ENAbled 1",
            "SYSTem:COMMunicate:LAN:APPLY 1",
        ]
        assert device.disconnect_calls == 1
        assert device.connect_attempts >= 1
        assert runner.last_request is not None

        programming = [
            p
            for p in progress_events
            if p.state == FirmwareUpdateState.PROGRAMMING
            and p.percent_complete > 20
        ]
        assert programming, "expected at least one PROGRAMMING progress > 20%"
        complete = [
            p for p in progress_events if p.state == FirmwareUpdateState.COMPLETE
        ]
        assert complete and complete[-1].percent_complete == pytest.approx(100.0)

    def test_when_process_fails_throws_with_programming_state(self) -> None:
        device = _FakeStreamingDevice("fake-port-8")
        runner = _FakeExternalProcessRunner(
            ExternalProcessResult(
                exit_code=1,
                timed_out=False,
                duration_seconds=0.01,
                standard_output_lines=("some output",),
                standard_error_lines=("fatal",),
            )
        )

        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert exc_info.value.failed_state == FirmwareUpdateState.PROGRAMMING
        assert service.current_state == FirmwareUpdateState.FAILED

    def test_when_process_times_out_throws(self) -> None:
        device = _FakeStreamingDevice("fake-port-9")
        runner = _FakeExternalProcessRunner(
            ExternalProcessResult(
                exit_code=0,
                timed_out=True,
                duration_seconds=2.0,
                standard_output_lines=tuple(),
                standard_error_lines=tuple(),
            )
        )
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert exc_info.value.failed_state == FirmwareUpdateState.PROGRAMMING
        assert isinstance(exc_info.value.__cause__, TimeoutError)

    def test_when_programming_failure_string_in_output_throws(self) -> None:
        device = _FakeStreamingDevice("fake-port-10")
        runner = _FakeExternalProcessRunner(
            ExternalProcessResult(
                exit_code=0,
                timed_out=False,
                duration_seconds=0.01,
                standard_output_lines=(
                    "writing...",
                    "Programming device failed at offset 0x1000",
                ),
                standard_error_lines=tuple(),
            )
        )
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert exc_info.value.failed_state == FirmwareUpdateState.PROGRAMMING
        assert isinstance(exc_info.value.__cause__, IOError)

    def test_when_device_version_matches_latest_skips_flash(self) -> None:
        wifi_release = FirmwareReleaseInfo(
            version=FirmwareVersion(19, 5, 4),
            tag_name="19.5.4",
            is_pre_release=False,
        )
        device = _FakeLanChipInfoStreamingDevice(
            "fake-port-9",
            chip_info=LanChipInfo(
                chip_id=1234,
                fw_version="19.5.4",
                build_date="Jan  8 2019",
            ),
        )

        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(
                latest_wifi_release=wifi_release
            ),
            external_process_runner=_FakeExternalProcessRunner(),
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        progress_events: List[FirmwareUpdateProgress] = []
        firmware_dir = _make_temp_wifi_dir()
        try:
            service.update_wifi_module(
                device, firmware_dir, progress=progress_events.append
            )
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert service.current_state == FirmwareUpdateState.COMPLETE
        assert "SYSTem:COMMUnicate:LAN:FWUpdate" not in device.sent_commands
        complete = [
            p for p in progress_events if p.state == FirmwareUpdateState.COMPLETE
        ]
        assert complete and complete[-1].percent_complete == pytest.approx(100.0)
        assert "already up to date" in complete[-1].current_operation.lower()

    def test_when_device_version_older_proceeds_with_flash(self) -> None:
        wifi_release = FirmwareReleaseInfo(
            version=FirmwareVersion(19, 6, 1),
            tag_name="19.6.1",
            is_pre_release=False,
        )
        device = _FakeLanChipInfoStreamingDevice(
            "fake-port-10",
            chip_info=LanChipInfo(
                chip_id=1234,
                fw_version="19.5.4",
                build_date="Jan  8 2019",
            ),
        )
        runner = _FakeExternalProcessRunner()
        options = _fast_options()
        options.post_lan_firmware_mode_delay = 0.005
        options.post_wifi_reconnect_delay = 0.005

        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(
                latest_wifi_release=wifi_release
            ),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=options,
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert service.current_state == FirmwareUpdateState.COMPLETE
        assert "SYSTem:COMMUnicate:LAN:FWUpdate" in device.sent_commands

    def test_when_device_does_not_support_lan_query_proceeds_with_flash(
        self,
    ) -> None:
        device = _FakeStreamingDevice("fake-port-11")  # no LanChipInfoProvider
        runner = _FakeExternalProcessRunner()
        options = _fast_options()
        options.post_lan_firmware_mode_delay = 0.005
        options.post_wifi_reconnect_delay = 0.005

        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=runner,
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=options,
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert service.current_state == FirmwareUpdateState.COMPLETE
        assert "SYSTem:COMMUnicate:LAN:FWUpdate" in device.sent_commands

    def test_rejects_empty_firmware_path(self) -> None:
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )
        with pytest.raises(ValueError):
            service.update_wifi_module(_FakeStreamingDevice(), "")

    def test_rejects_missing_firmware_path(self) -> None:
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )
        with pytest.raises(FileNotFoundError):
            service.update_wifi_module(_FakeStreamingDevice(), "/no/such/dir/xyz")


# ---- State machine ------------------------------------------------------


class TestStateMachine:
    def test_state_changed_callback_fires(self) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        hid.enqueue_read(bytes([0x01, 0x10]))
        hid.enqueue_read(bytes([0x01, 0x02]))
        hid.enqueue_read(bytes([0x01, 0x03]))
        hid.enqueue_read(bytes([0x01, 0x10]))

        info = HidDeviceInfo(0x04D8, 0x003C, "path-1", "SN-1", "Bootloader")
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[], [info]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        events: List[FirmwareUpdateStateChangedEventArgs] = []
        service.state_changed = events.append

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert any(
            e.current_state == FirmwareUpdateState.COMPLETE for e in events
        )
        assert all(e.previous_state != e.current_state for e in events)

    def test_back_to_back_updates_reset_from_complete(self) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        hid = _FakeHidTransport()
        for _ in range(2):
            hid.enqueue_read(bytes([0x01, 0x10]))
            hid.enqueue_read(bytes([0x01, 0x02]))
            hid.enqueue_read(bytes([0x01, 0x03]))
            hid.enqueue_read(bytes([0x01, 0x10]))

        info = HidDeviceInfo(0x04D8, 0x003C, "path-1", "SN-1", "Bootloader")
        service = _build_pic32_service(
            hid,
            _FakeHidEnumerator([[], [info], [], [info]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            service.update_firmware(device, hex_path)
            assert service.current_state == FirmwareUpdateState.COMPLETE
            service.update_firmware(device, hex_path)
            assert service.current_state == FirmwareUpdateState.COMPLETE
        finally:
            os.remove(hex_path)

    def test_close_blocks_further_updates(self) -> None:
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )
        service.close()
        hex_path = _make_temp_hex()
        try:
            with pytest.raises(RuntimeError):
                service.update_firmware(_FakeStreamingDevice(), hex_path)
        finally:
            os.remove(hex_path)

    def test_close_is_idempotent(self) -> None:
        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
        )
        service.close()
        service.close()  # no raise


# ---- Disconnected device ------------------------------------------------


class TestDisconnectedDevice:
    def test_update_firmware_when_device_disconnected_throws(self) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        device.is_connected = False

        info = HidDeviceInfo(0x04D8, 0x003C, "path-1", "SN-1", "Bootloader")
        service = _build_pic32_service(
            _FakeHidTransport(),
            _FakeHidEnumerator([[info]]),
            _FakeBootloaderProtocol([bytes([0xA1])]),
        )

        hex_path = _make_temp_hex()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_firmware(device, hex_path)
        finally:
            os.remove(hex_path)

        assert exc_info.value.failed_state == FirmwareUpdateState.PREPARING_DEVICE

    def test_update_wifi_when_device_disconnected_throws(self) -> None:
        device = _FakeStreamingDevice("fake-port-3")
        device.is_connected = False

        service = DefaultFirmwareUpdateService(
            hid_transport=_FakeHidTransport(),
            firmware_download_service=_FakeFirmwareDownloadService(),
            external_process_runner=_FakeExternalProcessRunner(),
            bootloader_protocol=_FakeBootloaderProtocol([bytes([0x10])]),
            hid_device_enumerator=_FakeHidEnumerator([]),
            options=_fast_options(),
        )

        firmware_dir = _make_temp_wifi_dir()
        try:
            with pytest.raises(FirmwareUpdateException) as exc_info:
                service.update_wifi_module(device, firmware_dir)
        finally:
            shutil.rmtree(firmware_dir, ignore_errors=True)

        assert exc_info.value.failed_state == FirmwareUpdateState.PREPARING_DEVICE
        # Reported as disconnected on every host: building the request first
        # failed on CI runners without cmd.exe, at IDLE, before the check.
        assert "must be connected" in str(exc_info.value.__cause__)
        assert device.sent_commands == []
