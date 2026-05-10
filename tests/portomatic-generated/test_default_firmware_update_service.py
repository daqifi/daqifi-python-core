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
from daqifi.firmware.bootloader import BootloaderProtocol
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
    ) -> None:
        self._connect_calls += 1
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
    def __init__(self, exception: BaseException) -> None:
        self._exception = exception

    def enumerate(
        self,
        vendor_id: Optional[int] = None,
        product_id: Optional[int] = None,
    ) -> Sequence[HidDeviceInfo]:
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
                standard_output_lines=tuple(),
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


class _FakeLanChipInfoStreamingDevice(_FakeStreamingDevice):
    def __init__(self, name: str, chip_info: LanChipInfo) -> None:
        super().__init__(name)
        self._chip_info = chip_info

    def get_lan_chip_info(self) -> Optional[LanChipInfo]:
        return self._chip_info


# ---- Test scaffolding ----------------------------------------------------


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
        enumerator = _FakeHidEnumerator([[info]])
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
        enumerator = _ThrowingHidEnumerator(
            RuntimeError("HID backend unavailable.")
        )
        options = _fast_options()

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

        cause = exc.__cause__
        assert isinstance(cause, RuntimeError)
        msg = str(cause)
        assert "HID enumeration failed while searching for bootloader device" in msg
        assert "VID=0x04D8" in msg
        assert "PID=0x003C" in msg

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


class TestUpdateWifiModule:
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
        assert device.sent_commands == [
            "SYSTem:COMMUnicate:LAN:FWUpdate",
            "SYSTem:COMMunicate:LAN:ENAbled 1",
            "SYSTem:COMMunicate:LAN:APPLY",
            "SYSTem:COMMunicate:LAN:SAVE",
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
            _FakeHidEnumerator([[info]]),
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
            _FakeHidEnumerator([[info], [info]]),
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
