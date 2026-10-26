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
        assert runner.last_request.arguments[0] == "/c"
        assert "winc_flash_tool.cmd" in runner.last_request.arguments[1]

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

        assert "/p TEST-PORT-7" in runner.last_request.arguments[1]
        assert "DaqifiDevice" not in runner.last_request.arguments[1]

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

        command = runner.last_request.arguments[1]
        assert os.path.join(firmware_dir, "winc_flash_tool.cmd") in command
        assert runner.last_request.working_directory == firmware_dir

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
