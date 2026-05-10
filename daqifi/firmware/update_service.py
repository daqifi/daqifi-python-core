"""Firmware update orchestrator — Protocol + concrete implementation.

Ports the C# ``IFirmwareUpdateService`` interface and the
``FirmwareUpdateService`` concrete class from
``Daqifi.Core.Firmware``. Sync per project convention —
implementations block until the device responds.

Divergences from upstream:

- Methods drop the ``Async`` suffix and return ``None`` instead of
  ``Task`` (sync end-to-end, mirrors ``NetworkConfigurable``,
  ``SdCardOperations``, etc.).
- The C# ``event StateChanged`` becomes a ``state_changed`` callable
  attribute — implementations expose it as an
  ``Optional[Callable[[FirmwareUpdateStateChangedEventArgs], None]]``
  slot the caller can set / clear. One handler at a time; multiplex
  with a fan-out wrapper if multiple observers need it.
- ``progress`` is typed ``Optional[Callable[[FirmwareUpdateProgress],
  None]]`` rather than ``IProgress<T>``.
- C# ``CancellationToken`` becomes ``Optional[threading.Event]``.
  Set the event to request cooperative cancellation; long-running
  loops poll between iterations.
- Per-state timeouts are enforced with monotonic deadline polling
  (no thread / linked CancellationTokenSource pair). Operations that
  perform a single blocking I/O call rely on the underlying
  transport's own timeout knob.
- ``IFirmwareDownloadService`` is async upstream AND in this
  codebase; the WiFi version-check call is bridged through
  ``asyncio.run`` so the surface stays sync.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import threading
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.bootloader import (
    BootloaderProtocol,
    Pic32BootloaderProtocol,
)
from daqifi.firmware.download_service import FirmwareDownloadService
from daqifi.firmware.external_process import (
    ExternalProcessRequest,
    ExternalProcessResult,
    ExternalProcessRunner,
)
from daqifi.firmware.lan_chip_info_provider import LanChipInfoProvider
from daqifi.firmware.update_events import (
    FirmwareUpdateException,
    FirmwareUpdateProgress,
    FirmwareUpdateStateChangedEventArgs,
)
from daqifi.firmware.update_options import FirmwareUpdateServiceOptions
from daqifi.firmware.version import FirmwareVersion


if TYPE_CHECKING:
    from daqifi._internal.comm import HidDeviceEnumerator, HidDeviceInfo, HidTransport
    from daqifi.devices import DaqifiStreamingDevice


__all__ = ["FirmwareUpdateService", "DefaultFirmwareUpdateService"]


_log = logging.getLogger(__name__)


_PROGRESS_CALLBACK = Optional[Callable[[FirmwareUpdateProgress], None]]
_STATE_CALLBACK = Optional[
    Callable[[FirmwareUpdateStateChangedEventArgs], None]
]


_PERCENT_RE = re.compile(r"(?P<percent>\d{1,3})\s*%")


# State-machine guard table (mirrors C# ``AllowedTransitions`` dict).
_ALLOWED_TRANSITIONS: dict = {
    FirmwareUpdateState.IDLE: {
        FirmwareUpdateState.PREPARING_DEVICE,
        FirmwareUpdateState.COMPLETE,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.PREPARING_DEVICE: {
        FirmwareUpdateState.WAITING_FOR_BOOTLOADER,
        FirmwareUpdateState.PROGRAMMING,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.WAITING_FOR_BOOTLOADER: {
        FirmwareUpdateState.CONNECTING,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.CONNECTING: {
        FirmwareUpdateState.ERASING_FLASH,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.ERASING_FLASH: {
        FirmwareUpdateState.PROGRAMMING,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.PROGRAMMING: {
        FirmwareUpdateState.VERIFYING,
        FirmwareUpdateState.JUMPING_TO_APP,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.VERIFYING: {
        FirmwareUpdateState.JUMPING_TO_APP,
        FirmwareUpdateState.COMPLETE,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.JUMPING_TO_APP: {
        FirmwareUpdateState.COMPLETE,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.COMPLETE: {FirmwareUpdateState.IDLE},
    FirmwareUpdateState.FAILED: {FirmwareUpdateState.IDLE},
}


@runtime_checkable
class FirmwareUpdateService(Protocol):
    """Protocol for orchestrating firmware updates on a DAQiFi device.

    Pythonic name for the C# ``IFirmwareUpdateService`` interface
    (the ``I`` prefix is dropped to match the rest of this codebase's
    Protocols).

    All methods are synchronous. Concrete implementations block until
    the update finishes (or raises).
    """

    @property
    def current_state(self) -> FirmwareUpdateState:
        """The orchestrator's current update state."""
        ...

    state_changed: _STATE_CALLBACK
    """Optional observer callback invoked on every state transition.

    Set to a callable to observe; set to ``None`` to detach.
    Implementations should not synchronously invoke update operations
    on the same service instance from inside this callback.
    """

    def update_firmware(
        self,
        device: "DaqifiStreamingDevice",
        hex_file_path: str,
        progress: _PROGRESS_CALLBACK = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """Run a PIC32 firmware update via bootloader mode.

        **Caller responsibility:** this is a destructive operation
        (flashes firmware over USB-HID). Callers driving multi-device
        rigs MUST gate the call against ``device.metadata.serial_number``
        per ``CLAUDE.md`` ("Identity-gating destructive operations in
        your own code"). The library does not enforce identity itself
        because the expected value is a property of the caller's rig
        configuration, not of the device transport.

        Raises:
            FirmwareUpdateException: when any update step fails.
        """
        ...

    def update_wifi_module(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: _PROGRESS_CALLBACK = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """Run a WiFi-module update via the external flashing tool.

        **Caller responsibility:** same identity-gating as
        :meth:`update_firmware`.

        Raises:
            FirmwareUpdateException: when any update step fails.
        """
        ...


class _Cancellation:
    """Per-state deadline + cooperative-cancel polling helper."""

    def __init__(
        self,
        state: FirmwareUpdateState,
        operation: str,
        timeout_seconds: float,
        cancel_event: Optional[threading.Event],
        timeout_message_builder: Callable[[float], str],
    ) -> None:
        self.state = state
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        self._end = time.monotonic() + timeout_seconds
        self._cancel_event = cancel_event
        self._timeout_message_builder = timeout_message_builder

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    @property
    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())

    def check(self) -> None:
        if self.cancel_requested:
            raise _CancelledError()
        if time.monotonic() >= self._end:
            raise TimeoutError(self._timeout_message_builder(self.timeout_seconds))


class _CancelledError(Exception):
    """Internal sentinel for cooperative cancellation."""


class DefaultFirmwareUpdateService:
    """Default firmware update orchestrator (PIC32 + WiFi).

    Concrete port of the C# ``FirmwareUpdateService`` class. Holds
    one ``threading.Lock`` so concurrent ``update_firmware`` /
    ``update_wifi_module`` calls serialize cleanly (mirrors the
    upstream ``SemaphoreSlim(1, 1)``).
    """

    def __init__(
        self,
        hid_transport: "HidTransport",
        firmware_download_service: FirmwareDownloadService,
        external_process_runner: ExternalProcessRunner,
        bootloader_protocol: Optional[BootloaderProtocol] = None,
        hid_device_enumerator: Optional["HidDeviceEnumerator"] = None,
        options: Optional[FirmwareUpdateServiceOptions] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if hid_transport is None:
            raise TypeError("hid_transport must not be None")
        if firmware_download_service is None:
            raise TypeError("firmware_download_service must not be None")
        if external_process_runner is None:
            raise TypeError("external_process_runner must not be None")

        self._hid_transport = hid_transport
        self.firmware_download_service = firmware_download_service
        self._external_process_runner = external_process_runner
        self._logger = logger if logger is not None else _log
        self._bootloader_protocol = (
            bootloader_protocol
            if bootloader_protocol is not None
            else Pic32BootloaderProtocol()
        )
        self._hid_device_enumerator = hid_device_enumerator
        self._options = options if options is not None else FirmwareUpdateServiceOptions()
        self._options.validate()

        self._operation_lock = threading.Lock()
        self._current_state = FirmwareUpdateState.IDLE
        self.state_changed: _STATE_CALLBACK = None
        self._current_operation = "Idle"
        self._last_reported_percent = 0.0
        self._bootloader_poll_attempts = 0
        self._last_bootloader_enumeration_error: Optional[BaseException] = None
        self._closed = False

    # -- public API ------------------------------------------------------

    @property
    def current_state(self) -> FirmwareUpdateState:
        return self._current_state

    def update_firmware(
        self,
        device: "DaqifiStreamingDevice",
        hex_file_path: str,
        progress: _PROGRESS_CALLBACK = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        if device is None:
            raise TypeError("device must not be None")
        if hex_file_path is None or not str(hex_file_path).strip():
            raise ValueError("HEX file path cannot be empty.")
        if not os.path.isfile(hex_file_path):
            raise FileNotFoundError(
                f"Firmware HEX file was not found: {hex_file_path!r}"
            )

        hex_records = self._parse_hex_records(hex_file_path)
        total_bytes = sum(len(record) for record in hex_records)
        if total_bytes <= 0:
            raise ValueError(
                "Firmware HEX file did not contain any writable records."
            )

        self._run_exclusive(
            lambda: self._run_pic32_update(
                device, hex_records, total_bytes, progress, cancel_event
            ),
            cancel_event,
        )

    def update_wifi_module(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: _PROGRESS_CALLBACK = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        if device is None:
            raise TypeError("device must not be None")
        if firmware_path is None or not str(firmware_path).strip():
            raise ValueError("WiFi firmware path cannot be empty.")
        if not (os.path.isfile(firmware_path) or os.path.isdir(firmware_path)):
            raise FileNotFoundError(
                f"WiFi firmware path was not found: {firmware_path!r}"
            )

        self._run_exclusive(
            lambda: self._run_wifi_update(
                device, firmware_path, progress, cancel_event
            ),
            cancel_event,
        )

    def close(self) -> None:
        """Release internal resources. Idempotent.

        Mirrors C# ``IDisposable.Dispose``. After ``close()`` no further
        update calls are accepted. Acquires the operation lock so any
        in-flight update finishes before tearing down the HID transport.
        Disconnects AND closes the transport — ``HidTransport`` exposes
        both lifecycle methods and ``close()`` is what marks the
        underlying handle disposed.
        """
        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._safe_disconnect_hid()
            try:
                self._hid_transport.close()
            except Exception:
                self._logger.warning(
                    "Failed to close HID transport during cleanup",
                    exc_info=True,
                )

    # -- exclusive-access wrapper ---------------------------------------

    def _run_exclusive(
        self,
        operation: Callable[[], None],
        cancel_event: Optional[threading.Event],
    ) -> None:
        # Early-cancel intentionally NOT raised here: the inner flows
        # check `cancel_event.is_set()` at every loop iteration and wrap
        # `_CancelledError` in `FirmwareUpdateException`. Raising early
        # here would leak the internal sentinel and skip the FAILED
        # state transition.
        self._raise_if_closed()

        if not self._operation_lock.acquire(blocking=True):
            raise RuntimeError("Failed to acquire firmware-update lock.")
        try:
            self._reset_if_terminal_state()
            if self._current_state != FirmwareUpdateState.IDLE:
                raise RuntimeError(
                    f"Cannot start firmware update while service is in "
                    f"state {self._current_state.name}."
                )

            self._last_reported_percent = 0.0
            self._bootloader_poll_attempts = 0
            self._last_bootloader_enumeration_error = None
            operation()
        finally:
            self._operation_lock.release()

    # -- PIC32 flow ------------------------------------------------------

    def _run_pic32_update(
        self,
        device: "DaqifiStreamingDevice",
        hex_records: Sequence[bytes],
        total_bytes: int,
        progress: _PROGRESS_CALLBACK,
        cancel_event: Optional[threading.Event],
    ) -> None:
        try:
            self._transition_to_state(
                FirmwareUpdateState.PREPARING_DEVICE,
                "Preparing device for PIC32 firmware update.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.PREPARING_DEVICE,
                0,
                self._current_operation,
                0,
                total_bytes,
            )

            def _prepare(deadline: _Cancellation) -> None:
                _ensure_device_connected(device)
                if device.is_streaming:
                    device.stop_streaming()
                from daqifi._internal import scpi_commands

                device.send(scpi_commands.force_bootloader())
                self._sleep_with_cancel(
                    self._options.post_force_boot_delay, deadline
                )
                device.disconnect()

            self._execute_with_state_timeout(
                FirmwareUpdateState.PREPARING_DEVICE,
                "prepare the device for bootloader mode",
                _prepare,
                cancel_event,
            )

            self._transition_to_state(
                FirmwareUpdateState.WAITING_FOR_BOOTLOADER,
                "Waiting for HID bootloader device.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.WAITING_FOR_BOOTLOADER,
                5,
                self._current_operation,
                0,
                total_bytes,
            )

            hid_device = self._execute_with_state_timeout(
                FirmwareUpdateState.WAITING_FOR_BOOTLOADER,
                "wait for HID bootloader enumeration",
                self._wait_for_bootloader_device,
                cancel_event,
            )

            self._transition_to_state(
                FirmwareUpdateState.CONNECTING,
                "Connecting to HID bootloader.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.CONNECTING,
                10,
                self._current_operation,
                0,
                total_bytes,
            )

            self._execute_with_state_timeout(
                FirmwareUpdateState.CONNECTING,
                "connect HID transport",
                lambda deadline: self._connect_to_bootloader_with_retry(
                    hid_device, deadline
                ),
                cancel_event,
            )

            version = self._execute_with_state_timeout(
                FirmwareUpdateState.CONNECTING,
                "request bootloader version",
                self._request_bootloader_version,
                cancel_event,
            )

            self._logger.info("Bootloader version response: %s", version)

            self._transition_to_state(
                FirmwareUpdateState.ERASING_FLASH,
                "Erasing PIC32 flash.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.ERASING_FLASH,
                15,
                self._current_operation,
                0,
                total_bytes,
            )

            self._execute_with_state_timeout(
                FirmwareUpdateState.ERASING_FLASH,
                "erase flash",
                self._erase_flash_with_retry,
                cancel_event,
            )

            self._transition_to_state(
                FirmwareUpdateState.PROGRAMMING,
                "Programming flash records.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.PROGRAMMING,
                20,
                self._current_operation,
                0,
                total_bytes,
            )

            self._execute_with_state_timeout(
                FirmwareUpdateState.PROGRAMMING,
                "program flash records",
                lambda deadline: self._program_flash(
                    hex_records, total_bytes, progress, deadline
                ),
                cancel_event,
            )

            self._transition_to_state(
                FirmwareUpdateState.VERIFYING,
                "Verifying bootloader response.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.VERIFYING,
                92,
                self._current_operation,
                total_bytes,
                total_bytes,
            )

            verification_version = self._execute_with_state_timeout(
                FirmwareUpdateState.VERIFYING,
                "verify bootloader response",
                self._request_bootloader_version,
                cancel_event,
            )

            self._logger.info(
                "Bootloader verification response: %s", verification_version
            )

            self._transition_to_state(
                FirmwareUpdateState.JUMPING_TO_APP,
                "Jumping to application firmware.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.JUMPING_TO_APP,
                95,
                self._current_operation,
                total_bytes,
                total_bytes,
            )

            self._execute_with_state_timeout(
                FirmwareUpdateState.JUMPING_TO_APP,
                "jump to application and reconnect serial transport",
                lambda deadline: self._jump_to_application_and_reconnect(
                    device, deadline
                ),
                cancel_event,
            )

            self._transition_to_state(
                FirmwareUpdateState.COMPLETE,
                "PIC32 firmware update completed.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.COMPLETE,
                100,
                self._current_operation,
                total_bytes,
                total_bytes,
            )
        except _CancelledError as exc:
            failed_state = self._current_state
            failed_operation = "PIC32 firmware update canceled."
            self._transition_to_state(
                FirmwareUpdateState.FAILED, failed_operation
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.FAILED,
                self._last_reported_percent,
                self._current_operation,
                0,
                total_bytes,
            )
            self._logger.warning("PIC32 firmware update canceled.")
            raise self._create_firmware_update_exception(
                failed_state, failed_operation, exc
            ) from exc
        except Exception as exc:
            failed_state = self._current_state
            failed_operation = self._current_operation
            self._transition_to_state(
                FirmwareUpdateState.FAILED, failed_operation
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.FAILED,
                self._last_reported_percent,
                failed_operation,
                0,
                total_bytes,
            )
            self._logger.error(
                "PIC32 firmware update failed in state %s: %s",
                failed_state.name,
                exc,
            )
            raise self._create_firmware_update_exception(
                failed_state, failed_operation, exc
            ) from exc
        finally:
            self._safe_disconnect_hid()

    # -- WiFi flow -------------------------------------------------------

    def _run_wifi_update(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: _PROGRESS_CALLBACK,
        cancel_event: Optional[threading.Event],
    ) -> None:
        total_bytes = 100

        try:
            if self._is_wifi_firmware_up_to_date(device, progress, cancel_event):
                return

            self._transition_to_state(
                FirmwareUpdateState.PREPARING_DEVICE,
                "Preparing device for WiFi module update.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.PREPARING_DEVICE,
                0,
                self._current_operation,
                0,
                total_bytes,
            )

            def _prepare(deadline: _Cancellation) -> None:
                _ensure_device_connected(device)
                if device.is_streaming:
                    device.stop_streaming()
                from daqifi._internal import scpi_commands

                device.send(scpi_commands.set_lan_firmware_update_mode())
                self._sleep_with_cancel(
                    self._options.post_lan_firmware_mode_delay, deadline
                )
                device.disconnect()

            self._execute_with_state_timeout(
                FirmwareUpdateState.PREPARING_DEVICE,
                "prepare device for WiFi update mode",
                _prepare,
                cancel_event,
            )

            self._transition_to_state(
                FirmwareUpdateState.PROGRAMMING,
                "Running WiFi module flash tool.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.PROGRAMMING,
                20,
                self._current_operation,
                0,
                total_bytes,
            )

            request = self._build_wifi_process_request(
                device, firmware_path, progress
            )
            process_result = self._execute_with_state_timeout(
                FirmwareUpdateState.PROGRAMMING,
                "execute WiFi flash process",
                lambda deadline: self._external_process_runner.run(request),
                cancel_event,
            )

            if process_result.timed_out:
                raise TimeoutError(
                    f"WiFi flashing process timed out after "
                    f"{request.timeout_seconds:.0f} seconds."
                )

            # External flashing tools commonly write failure strings to
            # either stdout or stderr depending on the tool / OS shell;
            # check both so we don't miss a "Programming device failed"
            # report that lands on stderr (divergence from upstream which
            # only checks stdout).
            if _contains_wifi_programming_failure(
                list(process_result.standard_output_lines)
                + list(process_result.standard_error_lines)
            ):
                raise IOError(
                    "WiFi flashing tool reported a programming failure."
                )

            if process_result.exit_code != 0:
                raise IOError(
                    f"WiFi flashing process exited with code "
                    f"{process_result.exit_code}. "
                    + _build_process_log_excerpt(process_result)
                )

            self._transition_to_state(
                FirmwareUpdateState.VERIFYING,
                "Reconnecting device and restoring LAN configuration.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.VERIFYING,
                92,
                self._current_operation,
                92,
                total_bytes,
            )

            def _reconnect(deadline: _Cancellation) -> None:
                self._sleep_with_cancel(
                    self._options.post_wifi_reconnect_delay, deadline
                )
                self._wait_for_serial_reconnect(device, deadline)
                from daqifi._internal import scpi_commands

                device.send(scpi_commands.enable_network_lan())
                device.send(scpi_commands.apply_network_lan())
                device.send(scpi_commands.save_network_lan())

            self._execute_with_state_timeout(
                FirmwareUpdateState.VERIFYING,
                "reconnect serial transport after WiFi flash",
                _reconnect,
                cancel_event,
            )

            self._transition_to_state(
                FirmwareUpdateState.COMPLETE,
                "WiFi module update completed.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.COMPLETE,
                100,
                self._current_operation,
                total_bytes,
                total_bytes,
            )
        except _CancelledError as exc:
            failed_state = self._current_state
            failed_operation = "WiFi module update canceled."
            self._transition_to_state(
                FirmwareUpdateState.FAILED, failed_operation
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.FAILED,
                self._last_reported_percent,
                self._current_operation,
                0,
                total_bytes,
            )
            self._logger.warning("WiFi module update canceled.")
            raise self._create_firmware_update_exception(
                failed_state, failed_operation, exc
            ) from exc
        except Exception as exc:
            failed_state = self._current_state
            failed_operation = self._current_operation
            self._transition_to_state(
                FirmwareUpdateState.FAILED, failed_operation
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.FAILED,
                self._last_reported_percent,
                failed_operation,
                0,
                total_bytes,
            )
            self._logger.error(
                "WiFi module update failed in state %s: %s",
                failed_state.name,
                exc,
            )
            raise self._create_firmware_update_exception(
                failed_state, failed_operation, exc
            ) from exc

    def _is_wifi_firmware_up_to_date(
        self,
        device: "DaqifiStreamingDevice",
        progress: _PROGRESS_CALLBACK,
        cancel_event: Optional[threading.Event],
    ) -> bool:
        if not isinstance(device, LanChipInfoProvider):
            return False

        try:
            chip_info = device.get_lan_chip_info()
        except Exception as exc:
            self._logger.debug(
                "Failed to query LAN chip info; proceeding with WiFi firmware "
                "update: %s",
                exc,
            )
            return False

        if chip_info is None:
            return False

        try:
            latest_wifi = _run_async(
                self.firmware_download_service.get_latest_wifi_release()
            )
        except Exception as exc:
            self._logger.debug(
                "Failed to query latest WiFi firmware release; proceeding "
                "with update: %s",
                exc,
            )
            return False

        if latest_wifi is None:
            return False

        if not _is_wifi_version_current(chip_info.fw_version, latest_wifi.tag_name):
            self._logger.info(
                "WiFi firmware update available: device has %s, latest is %s.",
                chip_info.fw_version,
                latest_wifi.tag_name,
            )
            return False

        message = (
            f"WiFi firmware is already up to date "
            f"(device: {chip_info.fw_version}, latest: {latest_wifi.tag_name})."
        )
        self._logger.info(message)
        self._transition_to_state(FirmwareUpdateState.COMPLETE, message)
        self._report_progress(
            progress, FirmwareUpdateState.COMPLETE, 100, message, 100, 100
        )
        return True

    def _build_wifi_process_request(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: _PROGRESS_CALLBACK,
    ) -> ExternalProcessRequest:
        tool_path = self._resolve_wifi_tool_path(firmware_path)
        port = self._resolve_wifi_port(device)

        tool_arguments = self._options.wifi_flash_tool_arguments_template
        tool_arguments = tool_arguments.replace("{port}", _quote_argument(port))
        tool_arguments = tool_arguments.replace(
            "{firmwarePath}", _quote_argument(firmware_path)
        )

        executable_path = tool_path
        executable_arguments = tool_arguments

        extension = os.path.splitext(tool_path)[1].lower()
        if extension in (".cmd", ".bat"):
            cmd_exe = shutil.which("cmd.exe")
            if cmd_exe is None:
                raise FileNotFoundError(
                    f"WiFi flash tool {tool_path!r} is a Windows .cmd/.bat "
                    "script but cmd.exe was not found on PATH. Run from "
                    "Windows or WSL with Windows interop enabled."
                )
            executable_path = cmd_exe
            executable_arguments = f'/c "{tool_path}" {tool_arguments}'

        progress_state = {"last_process_percent": 0}
        progress_lock = threading.Lock()

        def _on_stdout_line(line: str) -> None:
            self._logger.info("WiFi flash output: %s", line)
            parsed = _try_parse_wifi_tool_percent(line)
            if parsed is None:
                return

            process_percent = parsed
            with progress_lock:
                if process_percent < progress_state["last_process_percent"]:
                    process_percent = progress_state["last_process_percent"]
                else:
                    progress_state["last_process_percent"] = process_percent

            overall_percent = 20 + (process_percent * 0.70)
            self._report_progress(
                progress,
                FirmwareUpdateState.PROGRAMMING,
                overall_percent,
                line,
                process_percent,
                100,
            )

        def _on_stderr_line(line: str) -> None:
            self._logger.warning("WiFi flash stderr: %s", line)

        def _stdin_response(line: str) -> Optional[str]:
            if "power cycle winc and set to bootloader mode" in line.lower():
                return ""
            return None

        return ExternalProcessRequest(
            file_name=executable_path,
            arguments=executable_arguments,
            working_directory=os.path.dirname(tool_path) or None,
            timeout_seconds=self._options.wifi_process_timeout,
            on_standard_output_line=_on_stdout_line,
            on_standard_error_line=_on_stderr_line,
            standard_input_response_factory=_stdin_response,
        )

    def _resolve_wifi_tool_path(self, firmware_path: str) -> str:
        if os.path.isfile(firmware_path):
            return firmware_path

        if os.path.isdir(firmware_path):
            tool_name = self._options.wifi_flash_tool_file_name
            for root, _dirs, files in os.walk(firmware_path):
                for name in files:
                    if name == tool_name:
                        return os.path.join(root, name)
            raise FileNotFoundError(
                f"Could not locate {tool_name!r} under {firmware_path!r}."
            )

        raise FileNotFoundError(
            f"WiFi firmware path was not found: {firmware_path!r}"
        )

    def _resolve_wifi_port(self, device: "DaqifiStreamingDevice") -> str:
        override = self._options.wifi_port_override
        if override and override.strip():
            return override

        name = getattr(device, "name", None)
        if name and str(name).strip():
            return str(name)

        raise RuntimeError(
            "Unable to resolve a serial port name for WiFi update."
        )

    # -- bootloader I/O --------------------------------------------------

    def _parse_hex_records(self, hex_file_path: str) -> List[bytes]:
        with open(hex_file_path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        return list(self._bootloader_protocol.parse_hex_file(lines))

    def _wait_for_bootloader_device(
        self, deadline: _Cancellation
    ) -> "HidDeviceInfo":
        from daqifi._internal.hid_enumerator import HidLibraryDeviceEnumerator

        enumerator = self._hid_device_enumerator
        if enumerator is None:
            enumerator = HidLibraryDeviceEnumerator()
            self._hid_device_enumerator = enumerator

        while True:
            deadline.check()

            self._bootloader_poll_attempts += 1

            try:
                devices = enumerator.enumerate(
                    self._options.bootloader_vendor_id,
                    self._options.bootloader_product_id,
                )
                self._last_bootloader_enumeration_error = None
            except Exception as exc:
                # USB enumeration is transient by nature (kernel module
                # reloading, hidapi races, the device re-enumerating
                # after force_bootloader). Don't hard-fail on the first
                # error — retain it on _last_bootloader_enumeration_error
                # so the state-timeout message can surface it later, then
                # sleep + retry until the deadline expires.
                self._last_bootloader_enumeration_error = exc
                self._logger.debug(
                    "HID enumeration failed on poll attempt %d; will retry "
                    "until timeout: %s",
                    self._bootloader_poll_attempts,
                    exc,
                )
                self._sleep_with_cancel(self._options.poll_interval, deadline)
                continue

            for device in devices:
                return device

            self._sleep_with_cancel(self._options.poll_interval, deadline)

    def _connect_to_bootloader_with_retry(
        self,
        bootloader_device: "HidDeviceInfo",
        deadline: _Cancellation,
    ) -> None:
        def _attempt() -> None:
            if self._hid_transport.is_connected:
                self._hid_transport.disconnect()
            self._hid_transport.connect(
                self._options.bootloader_vendor_id,
                self._options.bootloader_product_id,
                bootloader_device.serial_number,
            )

        self._execute_with_retry(
            "connect HID bootloader",
            self._options.hid_connect_retry_count,
            self._options.hid_connect_retry_delay,
            _attempt,
            _is_transient_hid_error,
            deadline,
        )

    def _request_bootloader_version(
        self, deadline: _Cancellation
    ) -> str:
        from datetime import timedelta

        self._hid_transport.write(
            self._bootloader_protocol.create_request_version_message()
        )
        response = self._hid_transport.read(
            timedelta(seconds=self._options.bootloader_response_timeout)
        )
        version = self._bootloader_protocol.decode_version_response(response)
        if version.lower() == "error":
            raise ValueError("Bootloader returned an invalid version response.")
        return version

    def _erase_flash_with_retry(self, deadline: _Cancellation) -> None:
        from datetime import timedelta

        def _attempt() -> None:
            self._hid_transport.write(
                self._bootloader_protocol.create_erase_flash_message()
            )
            response = self._hid_transport.read(
                timedelta(seconds=self._options.bootloader_response_timeout)
            )
            if not self._bootloader_protocol.decode_erase_flash_response(response):
                raise ValueError(
                    "Bootloader erase acknowledgment was invalid."
                )

        self._execute_with_retry(
            "erase flash",
            self._options.flash_write_retry_count,
            self._options.flash_write_retry_delay,
            _attempt,
            _is_transient_hid_error,
            deadline,
        )

    def _program_flash(
        self,
        hex_records: Sequence[bytes],
        total_bytes: int,
        progress: _PROGRESS_CALLBACK,
        deadline: _Cancellation,
    ) -> None:
        from datetime import timedelta

        bytes_written = 0
        for index, record in enumerate(hex_records):
            deadline.check()

            def _attempt(record=record, index=index) -> None:
                message = self._bootloader_protocol.create_program_flash_message(
                    record
                )
                self._hid_transport.write(message)
                response = self._hid_transport.read(
                    timedelta(seconds=self._options.bootloader_response_timeout)
                )
                if not self._bootloader_protocol.decode_program_flash_response(
                    response
                ):
                    raise ValueError(
                        f"Bootloader program acknowledgment was invalid for "
                        f"record {index + 1}."
                    )

            self._execute_with_retry(
                f"program flash record {index + 1}",
                self._options.flash_write_retry_count,
                self._options.flash_write_retry_delay,
                _attempt,
                _is_transient_hid_error,
                deadline,
            )

            bytes_written += len(record)
            if total_bytes <= 0:
                completion = 90
            else:
                completion = 20 + (bytes_written / total_bytes * 70)
            self._report_progress(
                progress,
                FirmwareUpdateState.PROGRAMMING,
                completion,
                f"Programming record {index + 1} of {len(hex_records)}",
                bytes_written,
                total_bytes,
            )

    def _jump_to_application_and_reconnect(
        self,
        device: "DaqifiStreamingDevice",
        deadline: _Cancellation,
    ) -> None:
        self._hid_transport.write(
            self._bootloader_protocol.create_jump_to_application_message()
        )
        self._safe_disconnect_hid()
        self._wait_for_serial_reconnect(device, deadline)

    def _wait_for_serial_reconnect(
        self,
        device: "DaqifiStreamingDevice",
        deadline: _Cancellation,
    ) -> None:
        while True:
            deadline.check()

            if device.is_connected:
                return

            try:
                device.connect()
                if device.is_connected:
                    return
            except Exception as exc:
                self._logger.debug("Serial reconnect attempt failed: %s", exc)

            self._sleep_with_cancel(self._options.poll_interval, deadline)

    # -- retry / timeout helpers ----------------------------------------

    def _execute_with_retry(
        self,
        operation: str,
        max_attempts: int,
        retry_delay: float,
        action: Callable[[], None],
        is_transient: Callable[[BaseException], bool],
        deadline: _Cancellation,
    ) -> None:
        for attempt in range(1, max_attempts + 1):
            deadline.check()
            try:
                action()
                return
            except Exception as exc:
                if attempt >= max_attempts or not is_transient(exc):
                    raise
                self._logger.warning(
                    "Operation %r failed on attempt %d/%d; retrying in %.0f ms: %s",
                    operation,
                    attempt,
                    max_attempts,
                    retry_delay * 1000,
                    exc,
                )
                self._sleep_with_cancel(retry_delay, deadline)

    def _execute_with_state_timeout(
        self,
        state: FirmwareUpdateState,
        operation: str,
        action: Callable[["_Cancellation"], Any],
        cancel_event: Optional[threading.Event],
    ) -> Any:
        timeout = self._options.get_state_timeout(state)
        deadline = _Cancellation(
            state=state,
            operation=operation,
            timeout_seconds=timeout,
            cancel_event=cancel_event,
            timeout_message_builder=lambda t: self._build_state_timeout_message(
                state, operation, t
            ),
        )
        # Honour cancellation set BEFORE the action starts so we never
        # send a destructive command (force_bootloader, FWUpdate) on an
        # already-cancelled run.
        deadline.check()
        return action(deadline)

    def _build_state_timeout_message(
        self,
        state: FirmwareUpdateState,
        operation: str,
        timeout: float,
    ) -> str:
        message = (
            f"State '{state.name}' timed out while attempting to {operation} "
            f"after {timeout:.1f} seconds."
        )
        if state != FirmwareUpdateState.WAITING_FOR_BOOTLOADER:
            return message

        details = (
            f"No matching HID bootloader device was enumerated for "
            f"VID=0x{self._options.bootloader_vendor_id:04X}, "
            f"PID=0x{self._options.bootloader_product_id:04X} after "
            f"{self._bootloader_poll_attempts} poll attempt(s)."
        )

        if self._last_bootloader_enumeration_error is None:
            return f"{message} {details}"

        error_summary = _format_exception_summary(
            self._last_bootloader_enumeration_error
        )
        return f"{message} {details} Last HID enumeration error: {error_summary}."

    def _sleep_with_cancel(
        self, seconds: float, deadline: _Cancellation
    ) -> None:
        if seconds <= 0:
            deadline.check()
            return

        end = time.monotonic() + seconds
        while True:
            deadline.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.05))

    # -- state transitions / progress -----------------------------------

    def _report_progress(
        self,
        progress: _PROGRESS_CALLBACK,
        state: FirmwareUpdateState,
        percent_complete: float,
        current_operation: str,
        bytes_written: int,
        total_bytes: int,
    ) -> None:
        clamped = max(0.0, min(100.0, float(percent_complete)))
        self._last_reported_percent = clamped

        if progress is None:
            return

        try:
            progress(
                FirmwareUpdateProgress(
                    state=state,
                    percent_complete=clamped,
                    current_operation=current_operation,
                    bytes_written=max(0, bytes_written),
                    total_bytes=max(0, total_bytes),
                )
            )
        except Exception as exc:
            self._logger.debug("Progress callback raised: %s", exc)

    def _transition_to_state(
        self, next_state: FirmwareUpdateState, operation: str
    ) -> None:
        if self._current_state == next_state:
            self._current_operation = operation
            return

        allowed = _ALLOWED_TRANSITIONS.get(self._current_state, set())
        if next_state not in allowed:
            raise RuntimeError(
                f"Invalid firmware update transition: "
                f"{self._current_state.name} -> {next_state.name}."
            )

        previous_state = self._current_state
        self._current_state = next_state
        self._current_operation = operation

        self._logger.info(
            "Firmware update state transition: %s -> %s (%s)",
            previous_state.name,
            next_state.name,
            operation,
        )

        callback = self.state_changed
        if callback is None:
            return

        try:
            callback(
                FirmwareUpdateStateChangedEventArgs(
                    previous_state=previous_state,
                    current_state=next_state,
                    operation=operation,
                )
            )
        except Exception as exc:
            self._logger.debug("state_changed callback raised: %s", exc)

    def _reset_if_terminal_state(self) -> None:
        if self._current_state in (
            FirmwareUpdateState.COMPLETE,
            FirmwareUpdateState.FAILED,
        ):
            self._transition_to_state(
                FirmwareUpdateState.IDLE,
                "Resetting state for next firmware update operation.",
            )

    def _safe_disconnect_hid(self) -> None:
        if not self._hid_transport.is_connected:
            return
        try:
            self._hid_transport.disconnect()
        except Exception:
            self._logger.warning(
                "Failed to disconnect HID transport during cleanup",
                exc_info=True,
            )

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError("FirmwareUpdateService has been closed.")

    def _create_firmware_update_exception(
        self,
        failed_state: FirmwareUpdateState,
        failed_operation: str,
        exception: BaseException,
    ) -> FirmwareUpdateException:
        if isinstance(exception, FirmwareUpdateException):
            return exception
        recovery_guidance = _build_recovery_guidance(failed_state)
        message = (
            f"Firmware update failed in state '{failed_state.name}' "
            f"while {failed_operation}."
        )
        return FirmwareUpdateException(
            failed_state=failed_state,
            operation=failed_operation,
            message=message,
            recovery_guidance=recovery_guidance,
            cause=exception,
        )


# -- module-level helpers ------------------------------------------------


def _ensure_device_connected(device: Any) -> None:
    if not getattr(device, "is_connected", False):
        raise RuntimeError(
            "Device must be connected before starting firmware update."
        )


def _is_transient_hid_error(exc: BaseException) -> bool:
    return isinstance(exc, (IOError, OSError, TimeoutError, RuntimeError, ValueError))


def _build_recovery_guidance(state: FirmwareUpdateState) -> str:
    return {
        FirmwareUpdateState.PREPARING_DEVICE:
            "Ensure the device is connected over USB and not currently busy "
            "streaming.",
        FirmwareUpdateState.WAITING_FOR_BOOTLOADER:
            "The device did not enter bootloader mode. Try unplugging/replugging "
            "USB, then retry.",
        FirmwareUpdateState.CONNECTING:
            "Bootloader was found but HID connection failed. Check USB cable "
            "stability and retry.",
        FirmwareUpdateState.ERASING_FLASH:
            "Flash erase failed. Retry update; if this persists, power-cycle the "
            "device and re-enter bootloader mode.",
        FirmwareUpdateState.PROGRAMMING:
            "Programming failed. Retry update while keeping USB connected; device "
            "may still be recoverable in bootloader mode.",
        FirmwareUpdateState.VERIFYING:
            "Verification failed. Retry update and confirm the expected firmware "
            "package was selected.",
        FirmwareUpdateState.JUMPING_TO_APP:
            "The device did not return to application mode. Power-cycle the "
            "device and reconnect.",
    }.get(
        state,
        "Retry the update. If repeated failures occur, reconnect the device "
        "and attempt manual bootloader recovery.",
    )


def _quote_argument(value: str) -> str:
    """Quote a single argument so it survives shell-style parsing.

    Windows ``cmd.exe`` does NOT honour ``\\"`` as an escape — embedded
    double quotes have to be doubled (``""``). POSIX shells do the
    opposite. Pick the right rule based on ``os.name`` so the WiFi
    flash tool gets correctly-quoted paths regardless of host platform.
    """
    if value is None or value == "":
        return '""'

    if os.name == "nt":
        if any(ch in value for ch in (" ", "\t", '"')):
            return '"' + value.replace('"', '""') + '"'
        return value

    if any(ch in value for ch in (" ", "\t", '"')):
        escaped = value.replace('"', r'\"')
        return f'"{escaped}"'
    return value


def _try_parse_wifi_tool_percent(line: str) -> Optional[int]:
    if not line or not line.strip():
        return None

    match = _PERCENT_RE.search(line)
    if match:
        try:
            parsed = int(match.group("percent"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return max(0, min(100, parsed))

    lowered = line.lower()
    if "begin write operation" in lowered:
        return 33
    if "begin read operation" in lowered:
        return 66
    if "begin verify operation" in lowered:
        return 90
    return None


def _contains_wifi_programming_failure(lines: Iterable[str]) -> bool:
    for line in lines:
        if "programming device failed" in line.lower():
            return True
    return False


def _build_process_log_excerpt(result: ExternalProcessResult) -> str:
    excerpt: List[str] = []
    for line in list(result.standard_error_lines) + list(
        result.standard_output_lines
    ):
        if not line or not line.strip():
            continue
        excerpt.append(line)
        if len(excerpt) == 5:
            break

    if not excerpt:
        return "No process output captured."
    return f"Process output excerpt: {' | '.join(excerpt)}"


def _is_wifi_version_current(device_version: str, latest_tag_name: str) -> bool:
    device = FirmwareVersion.parse(device_version) if device_version else None
    latest = FirmwareVersion.parse(latest_tag_name) if latest_tag_name else None
    if device is None or latest is None:
        return False
    return device >= latest


def _format_exception_summary(exception: BaseException) -> str:
    parts: List[str] = []
    current: Optional[BaseException] = exception
    first = True
    while current is not None:
        prefix = "" if first else " | Inner "
        parts.append(f"{prefix}{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
        first = False
    return "".join(parts)


def _run_async(coro: Any) -> Any:
    """Bridge a single async call into sync code.

    Used for ``FirmwareDownloadService.get_latest_wifi_release`` —
    the download Protocol is async upstream, but the orchestrator's
    public surface is sync per this codebase's convention.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        raise RuntimeError(
            "FirmwareUpdateService cannot be invoked from within a "
            "running event loop. Run it from a sync thread."
        )
    return asyncio.run(coro)
