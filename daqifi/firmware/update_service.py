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
import shlex
import shutil
import subprocess
import threading
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    FrozenSet,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Union,
    runtime_checkable,
)

from daqifi._internal.usb_ports import list_usb_serial_ports
from daqifi._internal.usb_location import (
    canonical_port_name,
    hid_device_location,
    serial_port_location,
)
from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.bootloader import (
    BootloaderProtocol,
    FlashCrcRegion,
    Pic32BootloaderProtocol,
)
from daqifi.firmware.crc16 import Crc16
from daqifi.firmware.download_service import FirmwareDownloadService
from daqifi.firmware.external_process import (
    ExternalProcessRequest,
    ExternalProcessResult,
    ExternalProcessRunner,
)
from daqifi.firmware.lan_chip_info_provider import LanChipInfoProvider
from daqifi.firmware.update_events import (
    FirmwareUpdateCancelledError,
    FirmwareUpdateException,
    FirmwareUpdateProgress,
    FirmwareUpdateStateChangedEventArgs,
)
from daqifi.firmware.update_options import (
    DEFAULT_WIFI_FLASH_TOOL_ARGUMENTS_TEMPLATE,
    DEFAULT_WIFI_FLASH_TOOL_FILE_NAME,
    WIFI_BRIDGE_SETTLE_SECONDS,
    FirmwareUpdateServiceOptions,
)
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


# SubprocessRunner.run() can spend a few seconds AFTER its
# request.timeout_seconds doing cleanup (proc.kill() + unbounded
# proc.wait(), plus 2.0s join timeouts on the stdout/stderr pump
# threads). Reserve this much in _clamp_process_timeout so a
# timed-out subprocess still returns control inside the
# PROGRAMMING state-timeout budget.
_PROCESS_CLEANUP_BUFFER_S = 5.0


# Hard caps on the WiFi-tool directory walk to keep
# _resolve_wifi_tool_path bounded even on accidentally-huge trees.
# The walk runs BEFORE the PROGRAMMING state-timeout wrapper is
# entered, so without these caps a missing tool under a giant
# directory could hang update_wifi_module indefinitely.
_WIFI_TOOL_SCAN_MAX_DIRS = 500
_WIFI_TOOL_SCAN_MAX_FILES = 50_000


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
        FirmwareUpdateState.CLEANING_UP,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.PROGRAMMING: {
        FirmwareUpdateState.VERIFYING,
        FirmwareUpdateState.JUMPING_TO_APP,
        FirmwareUpdateState.CLEANING_UP,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.VERIFYING: {
        FirmwareUpdateState.JUMPING_TO_APP,
        FirmwareUpdateState.COMPLETE,
        FirmwareUpdateState.CLEANING_UP,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.CLEANING_UP: {
        FirmwareUpdateState.RECOVERED,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.JUMPING_TO_APP: {
        FirmwareUpdateState.COMPLETE,
        FirmwareUpdateState.FAILED,
    },
    FirmwareUpdateState.COMPLETE: {FirmwareUpdateState.IDLE},
    FirmwareUpdateState.FAILED: {FirmwareUpdateState.IDLE},
    FirmwareUpdateState.RECOVERED: {FirmwareUpdateState.IDLE},
}

# PIC32 states whose failure can leave flash half-written, so a re-erase
# follows (port of daqifi-core 7c4b014).
_CLEANUP_STATES = frozenset({
    FirmwareUpdateState.ERASING_FLASH,
    FirmwareUpdateState.PROGRAMMING,
    FirmwareUpdateState.VERIFYING,
})
_RECOVERED = "recovered"
_CLEANUP_FAILED = "cleanup_failed"


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
        per ``CONTRIBUTING.md`` ("Identity-gating destructive operations in
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
        skip_version_check: bool = False,
        firmware_version: Optional[str] = None,
    ) -> None:
        """Run a WiFi-module update via the external flashing tool.

        **Caller responsibility:** same identity-gating as
        :meth:`update_firmware`.

        Args:
            skip_version_check: flash even when the module already reports
                the latest version. Mirrors ``skipVersionCheck`` on the C#
                ``UpdateWifiModuleAsync``. Without it an up-to-date module
                cannot be reflashed at all, so there is no way to reinstall
                the current image — which is what validating the WiFi update
                path on hardware requires when every board is already current.
            firmware_version: the bundle's firmware version, substituted for
                ``{version}`` in ``wifi_flash_tool_arguments_template`` (the
                default template passes it as ``/v``). Pass the version
                :meth:`FirmwareDownloadService.download_wifi_firmware`
                returns with the path; when omitted it is read from the
                bundle.

                **Default tool and arguments: checked before anything is
                sent.** "Default" is by value: the tool that will run is a
                file named exactly ``winc_flash_tool.cmd`` and the template is
                exactly the shipped one. The update is then refused with
                ValueError, before the device is queried, unless the version
                is made of letters, digits, ``.``, ``_`` and ``-`` and
                ``files/winc1500/<version>/firmware/programmer_firmware.bin``
                (the image the tool opens) exists beside the tool -- when
                omitted, that folder must hold exactly one such version --
                and the port is made of letters, digits and ``. _ : / -``.
                The flash request checked there is the one that runs.

                **Any other tool or template:** ``{version}`` is the version
                given, or else the one version found across the bundle's
                ``files/winc*/`` folders (ValueError when there is not exactly
                one). It is substituted with the same checks as ``{port}`` and
                ``{firmwarePath}`` and nothing more: the arguments are not
                parsed, and making the tool use the version is the caller's
                job.

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
            raise FirmwareUpdateCancelledError()
        if time.monotonic() >= self._end:
            raise TimeoutError(self._timeout_message_builder(self.timeout_seconds))


_BRIDGE_POLL_SECONDS = 1.0
_APPLY_REPLY_WINDOW_MS = 1500
_BRIDGE_EXCHANGE_MAX_MS = 3000
_SUCCESS_CONSOLE_CHECKS = 5
_ERROR_QUEUE_DRAIN_LIMIT = 16
_NOT_RECOVERED = (
    " The device could not be confirmed out of WiFi update mode: it may be "
    "in USB-transparent bridge mode, or hold a pending update request; "
    "power-cycle it"
)


def _anchor_to_cwd(path: str) -> str:
    """Prefix a relative path with the working directory, nothing more.

    Not os.path.abspath: that also collapses ``..`` textually, and through
    a symlink ``link/../x`` names a different directory than ``x`` next to
    the link, so the tool could run from the wrong bundle.
    """
    return path if os.path.isabs(path) else os.path.join(os.getcwd(), path)


def _list_serial_port_names() -> Optional[FrozenSet[str]]:
    """Serial ports currently present, or None when they can't be listed.

    None, not an empty set: a failed listing is no evidence that a port
    disappeared.
    """
    try:
        from serial.tools import list_ports

        return frozenset(port.device for port in list_ports.comports())
    except Exception:
        return None


# Shared with discovery (daqifi._internal.usb_ports); kept under this name,
# which tests patch.
_list_usb_serial_ports = list_usb_serial_ports


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
        self._preexisting_bootloaders: FrozenSet[Tuple[Any, Any, Any]] = frozenset()
        self._target_port_name: Optional[str] = None
        self._target_port_seen_gone = False
        self._target_usb_location: Optional[str] = None
        self._selected_bootloader_location: Optional[str] = None
        self._rejected_by_location = 0
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
        # ⛔ BEFORE ANYTHING ELSE TOUCHES THE DEVICE (#254). This refusal
        # used to be incidental: PREPARING_DEVICE stopped streaming first --
        # a send the consumer thread try-acquires and wins when the exchange
        # is idle -- and only the next step's run_exclusive refused. The
        # caller got an exception AND a device whose stream had been
        # stopped. A refused update must leave the device untouched, so the
        # rule is asserted here, before the first byte.
        #
        # The refusal itself is right, not the defect: two steps later the
        # flow captures the console, which pauses and restarts the very
        # consumer thread a message-received callback runs on (#238). Do
        # not "fix" #254 by switching _send_and_settle to the owning
        # exchange form -- that deletes the refusal and walks the update
        # into the capture.
        on_consumer_thread = getattr(device, "_on_consumer_thread", None)
        if on_consumer_thread is not None and on_consumer_thread():
            raise RuntimeError(
                "update_firmware cannot run on the protobuf consumer thread, "
                "that is, from a message-received callback: the update "
                "captures the device's console, which pauses and restarts "
                "that very thread. Nothing was sent. Start the update from "
                "your own thread."
            )
        if hex_file_path is None or not str(hex_file_path).strip():
            raise ValueError("HEX file path cannot be empty.")
        if not os.path.isfile(hex_file_path):
            raise FileNotFoundError(
                f"Firmware HEX file was not found: {hex_file_path!r}"
            )

        hex_records, crc_regions = self._parse_hex_records(hex_file_path)
        total_bytes = sum(len(record) for record in hex_records)
        if total_bytes <= 0:
            raise ValueError(
                "Firmware HEX file did not contain any writable records."
            )

        self._run_exclusive(
            lambda: self._run_pic32_update(
                device, hex_records, total_bytes, progress, cancel_event,
                crc_regions,
            ),
            cancel_event,
        )

    def update_wifi_module(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: _PROGRESS_CALLBACK = None,
        cancel_event: Optional[threading.Event] = None,
        skip_version_check: bool = False,
        firmware_version: Optional[str] = None,
    ) -> None:
        if device is None:
            raise TypeError("device must not be None")
        # ⛔ BEFORE ANYTHING ELSE TOUCHES THE DEVICE -- the WiFi sibling of
        # #254. Nothing refused this at the entry, and the refusal that did
        # exist upstream was SWALLOWED: _is_wifi_firmware_up_to_date calls
        # get_lan_chip_info(), which the consumer thread is refused, but it
        # catches that and answers "not up to date". PREPARING_DEVICE then
        # stopped streaming (stop_streaming() and an unconditional STOP --
        # sends the consumer thread wins when the exchange is idle) and only
        # the next _send_and_settle refused: an exception AND a device whose
        # stream had been stopped. Asserted here, before the first byte.
        #
        # The refusal itself is right: the flow captures the console, which
        # pauses and restarts the very consumer thread a message-received
        # callback runs on (#238). Do not switch _send_and_settle to the
        # owning exchange form instead -- that deletes the refusal.
        on_consumer_thread = getattr(device, "_on_consumer_thread", None)
        if on_consumer_thread is not None and on_consumer_thread():
            raise RuntimeError(
                "update_wifi_module cannot run on the protobuf consumer "
                "thread, that is, from a message-received callback: the "
                "update captures the device's console, which pauses and "
                "restarts that very thread. Nothing was sent. Start the "
                "update from your own thread."
            )
        if firmware_path is None or not str(firmware_path).strip():
            raise ValueError("WiFi firmware path cannot be empty.")
        if not (os.path.isfile(firmware_path) or os.path.isdir(firmware_path)):
            raise FileNotFoundError(
                f"WiFi firmware path was not found: {firmware_path!r}"
            )
        plan = self._plan_wifi_update(
            device, firmware_path, progress, firmware_version
        )

        self._run_exclusive(
            lambda: self._run_wifi_update(
                device, firmware_path, progress, cancel_event,
                skip_version_check, plan,
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

    # -- device-exclusive send ------------------------------------------

    def _send_and_settle(
        self, device, message, delay: float, deadline: _Cancellation
    ) -> None:
        """Send a command the following delay depends on, then wait.

        ⛔ A BARE send IS NOT ENOUGH HERE, and the reason outlived the
        one it was written for. ``send()`` now blocks until the bytes are
        out, so the delay no longer risks starting before the command
        reaches the board. What remains is the DELAY itself: it is this
        command's settling time -- a reboot, a mode change -- and another
        flow's command landing inside it disturbs exactly the thing being
        waited for. Owning the exchange across both the send and the wait
        is what keeps the window clean.

        The service's own ``_operation_lock`` does NOT help: it serialises
        updates against each other, not against the device's other flows.
        """
        def _send_then_settle() -> None:
            device.send(message)
            # ⛔ UNCONDITIONALLY, EVEN AT delay == 0. `_sleep_with_cancel`
            # is not only a sleep: at `seconds <= 0` it checks
            # cancellation and returns, so it is also THE cancel check on
            # this path. An `if delay:` guard around it therefore skipped
            # the check whenever a settle delay was configured as 0 --
            # which validate() allows -- and a cancel set during power-on
            # was ignored: LAN:FWUpdate went out anyway and forced bridge
            # recovery.
            #
            # The helper was already written to do the right thing at 0.
            # The guard was an optimisation that defeated it.
            #
            # ⛔ INSIDE the ownership, not after it. The sleep used to run
            # once run_exclusive had RETURNED, so the docstring above
            # claimed a window the code did not hold: another thread's
            # POWer:STATe 0 landed mid-settle and the update aborted. The
            # settling time is this command's, so it is part of the
            # exchange.
            self._sleep_with_cancel(delay, deadline)

        device.run_exclusive(_send_then_settle)

    # -- exclusive-access wrapper ---------------------------------------

    def _run_exclusive(
        self,
        operation: Callable[[], None],
        cancel_event: Optional[threading.Event],
    ) -> None:
        # Early-cancel intentionally NOT raised here: the inner flows
        # check `cancel_event.is_set()` at every loop iteration and wrap
        # `FirmwareUpdateCancelledError` in `FirmwareUpdateException`. Raising early
        # here would leak the internal sentinel and skip the FAILED
        # state transition.
        self._raise_if_closed()

        with self._operation_lock:
            # Re-check after acquiring the lock: close() runs under the
            # same lock, so without this we could race past the first
            # _raise_if_closed() and start an update on an already-
            # closed service.
            self._raise_if_closed()

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

    # -- PIC32 flow ------------------------------------------------------

    def _run_pic32_update(
        self,
        device: "DaqifiStreamingDevice",
        hex_records: Sequence[bytes],
        total_bytes: int,
        progress: _PROGRESS_CALLBACK,
        cancel_event: Optional[threading.Event],
        crc_regions: Optional[Sequence[FlashCrcRegion]] = None,
    ) -> None:
        # Set when the first ERASE or PROGRAM goes out: only then can the
        # flash be half-written, and only then is a re-erase due.
        self._flash_touched = False
        self._crc_regions = crc_regions
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
                from daqifi._internal import scpi_commands

                # Taken before FORceBoot so the wait below can tell this
                # device's bootloader from one that was already enumerated,
                # and (where the OS exposes it) bind it to this device's
                # physical USB port. The serial ports are listed on both
                # sides of it: a board switching from its application to its
                # bootloader between two scans would otherwise be in neither.
                applications = self._list_application_ports(deadline)
                self._preexisting_bootloaders = (
                    self._snapshot_bootloader_devices(deadline)
                )
                applications |= self._list_application_ports(deadline)
                self._refuse_if_several_boards_attached(applications)
                if device.is_streaming:
                    device.stop_streaming()
                self._target_port_name = getattr(
                    getattr(device, "transport", None), "port_name", None
                )
                self._target_usb_location = serial_port_location(
                    self._target_port_name
                )
                self._target_port_seen_gone = False
                self._selected_bootloader_location = None
                self._send_and_settle(
                    device, scpi_commands.force_bootloader(),
                    self._options.post_force_boot_delay, deadline,
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
                "Verifying flash contents by CRC." if crc_regions
                else "Verifying bootloader response.",
            )
            self._report_progress(
                progress,
                FirmwareUpdateState.VERIFYING,
                92,
                self._current_operation,
                total_bytes,
                total_bytes,
            )

            self._execute_with_state_timeout(
                FirmwareUpdateState.VERIFYING,
                "verify flash contents",
                lambda deadline: self._verify_flash(crc_regions, deadline),
                cancel_event,
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
        except FirmwareUpdateCancelledError as exc:
            failed_state = self._current_state
            failed_operation = "PIC32 firmware update canceled."
            self._logger.warning("PIC32 firmware update canceled.")
            # A cancel mid-flash leaves the board half-written as surely as
            # an error does, so it is cleaned up the same way.
            outcome = self._end_failed_pic32_update(
                failed_state, failed_operation, progress, total_bytes
            )
            raise self._create_firmware_update_exception(
                failed_state, failed_operation, exc, outcome
            ) from exc
        except Exception as exc:
            failed_state = self._current_state
            failed_operation = self._current_operation
            self._logger.error(
                "PIC32 firmware update failed in state %s: %s",
                failed_state.name,
                exc,
            )
            outcome = self._end_failed_pic32_update(
                failed_state, failed_operation, progress, total_bytes
            )
            raise self._create_firmware_update_exception(
                failed_state, failed_operation, exc, outcome
            ) from exc
        finally:
            self._safe_disconnect_hid()

    def _end_failed_pic32_update(
        self,
        failed_state: FirmwareUpdateState,
        failed_operation: str,
        progress: _PROGRESS_CALLBACK,
        total_bytes: int,
    ) -> Optional[str]:
        """Take a failed PIC32 update to its terminal state.

        Once an ERASE or PROGRAM has been sent, a failure while erasing,
        programming or verifying can leave a
        half-written application, which the bootloader starts on the next
        reset if its reset address was written (bootloader.c,
        BOOTLOADER_CHECK_FOR_TRIGGER). Then, while the bootloader is still
        connected, the flash is re-erased and read back blank: CLEANING_UP,
        then RECOVERED, and the board stays in its bootloader. Port of
        daqifi-core 7c4b014, plus the read-back.

        Returns _RECOVERED, _CLEANUP_FAILED, or None when no re-erase was
        due (the failure didn't touch flash).
        """
        percent = self._last_reported_percent

        def _end(state: FirmwareUpdateState, operation: str) -> None:
            self._transition_to_state(state, operation)
            self._report_progress(progress, state, percent, operation, 0, total_bytes)

        if failed_state not in _CLEANUP_STATES or not self._flash_touched:
            # Nothing was erased or programmed (a cancel can land as
            # ERASING_FLASH is announced, before the erase goes out): the
            # application is intact and must not be erased now.
            _end(FirmwareUpdateState.FAILED, failed_operation)
            return None
        if not self._hid_transport.is_connected:
            self._logger.warning(
                "No re-erase after the failure in %s: the bootloader is no "
                "longer connected; the board may hold a half-written "
                "application.",
                failed_state.name,
            )
            _end(
                FirmwareUpdateState.FAILED,
                "Re-erase skipped: the bootloader is no longer connected; the "
                "board may hold a half-written application.",
            )
            return _CLEANUP_FAILED
        try:
            _end(
                FirmwareUpdateState.CLEANING_UP,
                "Re-erasing flash so the board isn't left with a half-written "
                "application.",
            )
            # A request may have gone unanswered (that may be the failure),
            # and its reply could arrive now: clear it before the erase.
            self._drain_hid_reports()
            # Its own deadline, without the caller's cancel event: a cancel
            # is what may have brought us here, and the re-erase must still
            # run.
            def _erase_and_confirm(deadline: _Cancellation) -> None:
                self._erase_flash_with_retry(deadline)
                self._confirm_flash_erased(deadline)

            self._execute_with_state_timeout(
                FirmwareUpdateState.CLEANING_UP,
                "re-erase flash after the failed update and confirm it blank",
                _erase_and_confirm,
                None,
            )
        except Exception as exc:
            self._logger.error(
                "Re-erase after the failure in %s failed: %s; the board may "
                "hold a half-written application.",
                failed_state.name,
                exc,
            )
            _end(
                FirmwareUpdateState.FAILED,
                "Re-erase after the failure failed or couldn't be confirmed; "
                "the board may hold a half-written application.",
            )
            return _CLEANUP_FAILED
        _end(
            FirmwareUpdateState.RECOVERED,
            "Flash re-erased; the board stays in its bootloader with no "
            "application.",
        )
        return _RECOVERED

    # -- WiFi flow -------------------------------------------------------

    def _run_wifi_update(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: _PROGRESS_CALLBACK,
        cancel_event: Optional[threading.Event],
        skip_version_check: bool = False,
        plan: Optional[_WifiPlan] = None,
    ) -> None:
        # "update_requested" is armed BEFORE the FWUpdate send: a send that
        # raises can still have been delivered. "recovered" is what
        # _try_leave_lan_update_mode last concluded (None: it never finished).
        bridge_state = {"update_requested": False, "recovered": None}
        try:
            self._run_wifi_update_steps(
                device, firmware_path, progress, cancel_event, bridge_state,
                skip_version_check, plan,
            )
        finally:
            # Whatever ended the update, and wherever an interrupt landed
            # (even inside the recovery a handler below started), it doesn't
            # stay in its last state, and an unconfirmed device is named.
            if self._current_state != FirmwareUpdateState.COMPLETE:
                if self._current_state != FirmwareUpdateState.FAILED:
                    self._transition_to_state(
                        FirmwareUpdateState.FAILED,
                        self._current_operation + _NOT_RECOVERED,
                    )
                if (
                    bridge_state["update_requested"]
                    and bridge_state["recovered"] is not True
                ):
                    self._logger.warning(
                        "The WiFi update ended without confirming the device "
                        "left WiFi update mode; it may be in USB-transparent "
                        "bridge mode, or hold a pending update request. "
                        "Power-cycle it."
                    )

    def _run_wifi_update_steps(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: _PROGRESS_CALLBACK,
        cancel_event: Optional[threading.Event],
        bridge_state: dict,
        skip_version_check: bool = False,
        plan: Optional[_WifiPlan] = None,
    ) -> None:
        total_bytes = 100

        try:
            # Honour cancellation BEFORE the WiFi version-check makes
            # an HTTP call to GitHub releases. Without this an already-
            # cancelled run still incurs a network round-trip and only
            # raises once it reaches the first state-transition point.
            _check_cancel(cancel_event)
            # `skip_version_check` reaches the version check, not the flash:
            # an up-to-date module is the ONLY thing it overrides, so every
            # later refusal (disconnected device, non-USB transport, a tool
            # that reports failure) still applies unchanged.
            if skip_version_check:
                self._logger.info(
                    "Skipping the WiFi firmware version check at the "
                    "caller's request; the module will be flashed even if it "
                    "already reports the latest version."
                )
            elif self._is_wifi_firmware_up_to_date(device, progress, cancel_event):
                return
            _check_cancel(cancel_event)

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

            # Built before the device is switched into WiFi-update mode, so
            # an unresolvable port, tool or cmd.exe fails while the device
            # is still untouched. A disconnected device is reported as such
            # first, whatever the host can or can't run.
            _ensure_device_connected(device)
            # The bridge is on the USB CDC port, and while it's up the board
            # can only be reached there: a device on TCP couldn't be brought
            # back out of it. Refused before anything is sent.
            if getattr(device, "is_usb_connection", True) is not True:
                raise RuntimeError(
                    "The WiFi module update needs the device connected over "
                    "USB serial: while the WiFi bridge is up, the board can "
                    "only be reached on its USB port."
                )
            request = self._wifi_request_for(
                device, firmware_path, progress, plan or _WifiPlan()
            )

            def _prepare(deadline: _Cancellation) -> None:
                _ensure_device_connected(device)
                # A cancel already requested must not still power the
                # module on or switch the device into update mode.
                deadline.check()
                if device.is_streaming:
                    device.stop_streaming()
                from daqifi._internal import scpi_commands

                # Stop the stream whatever is_streaming says: one started by
                # a raw send isn't tracked, and its output would fill every
                # text exchange below.
                device.send(scpi_commands.stop_streaming())

                # The WINC is off in standby and right after a PIC32
                # reflash, and LAN commands are refused then, so FWUpdate
                # would not take effect.
                if self._options.power_on_wifi_module_before_lan_update_mode:
                    self._send_and_settle(
                        device, scpi_commands.turn_device_on(),
                        self._options.power_on_wifi_module_settle_delay,
                        deadline,
                    )

                # In standby LAN:APPLY doesn't restart WiFi, so the bridge
                # can't come up, and FWUpdate's request would linger. Refuse
                # before anything is requested.
                if self._wifi_module_powered(device) is False:
                    raise RuntimeError(
                        "The device is in standby (SYSTem:POWer:STATe? 0), so "
                        "the WiFi bridge can't come up. Power it on, or leave "
                        "power_on_wifi_module_before_lan_update_mode enabled."
                    )

                # FWUpdate only REQUESTS the update (SCPI_LANFwUpdate sets
                # WIFI_FW_UPDATE_REQUESTED); the next LAN:APPLY is what
                # enters the bridge and turns USB-transparent mode on. This is
                # the firmware's documented sequence (firmware/utilities:
                # POWer:STATe 1, FWUpdate, APPLY, then the flash tool).
                bridge_state["update_requested"] = True
                self._send_and_settle(
                    device, scpi_commands.set_lan_firmware_update_mode(),
                    self._options.post_lan_firmware_mode_delay, deadline,
                )

            self._execute_with_state_timeout(
                FirmwareUpdateState.PREPARING_DEVICE,
                "prepare device for WiFi update mode",
                _prepare,
                cancel_event,
            )

            # The bridge has its own budget: on a board in STA retry backoff
            # its entry waits up to 30 s, longer than the PREPARING_DEVICE
            # timeout was sized for.
            bridge_deadline = _Cancellation(
                FirmwareUpdateState.PREPARING_DEVICE,
                "enter the WiFi bridge",
                WIFI_BRIDGE_SETTLE_SECONDS,
                cancel_event,
                lambda seconds: (
                    f"The WiFi bridge did not come up within {seconds:.0f} s "
                    "of LAN:APPLY, so the flash tool was not started."
                ),
            )
            self._enter_wifi_bridge(device, bridge_deadline)

            def _release_port(deadline: _Cancellation) -> None:
                device.disconnect()
                # The OS frees the COM port some time after close; the
                # flash tool can't open it before then.
                self._sleep_with_cancel(
                    self._options.post_lan_disconnect_port_release_delay,
                    deadline,
                )

            self._execute_with_state_timeout(
                FirmwareUpdateState.PREPARING_DEVICE,
                "release the serial port for the WiFi flash tool",
                _release_port,
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

            # Cap the external process's own timeout to the remaining
            # state deadline so the process can't outlive the
            # PROGRAMMING state timeout (the deadline.check() inside
            # _execute_with_state_timeout only fires AROUND the call,
            # not during the single blocking subprocess.wait).
            # Stash the clamped request so the TimeoutError message
            # below reports the timeout that was actually enforced, not
            # the unclamped request.timeout_seconds value.
            clamped_holder: dict = {}

            def _run(deadline: _Cancellation) -> ExternalProcessResult:
                clamped = _clamp_process_timeout(request, deadline.remaining)
                clamped_holder["request"] = clamped
                return self._external_process_runner.run(clamped)

            process_result = self._execute_with_state_timeout(
                FirmwareUpdateState.PROGRAMMING,
                "execute WiFi flash process",
                _run,
                cancel_event,
            )

            enforced_request = clamped_holder.get("request", request)
            if process_result.timed_out:
                raise TimeoutError(
                    f"WiFi flashing process timed out after "
                    f"{enforced_request.timeout_seconds:.0f} seconds."
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

            # The exit code is not reliable in either direction: a tool that
            # could not open the port can still exit 0. Its own success line
            # is the authority.
            if not _contains_marker(
                list(process_result.standard_output_lines)
                + list(process_result.standard_error_lines),
                self._options.wifi_flash_success_marker,
            ):
                raise IOError(
                    "WiFi flashing did not complete successfully: the flash "
                    "tool never reported "
                    f"'{self._options.wifi_flash_success_marker}'. "
                    + _build_process_log_excerpt(process_result)
                )

            # ⛔ #190 item 5: PAST THIS POINT A CANCEL IS TOO LATE TO HONOUR.
            #
            # The flash has SUCCEEDED -- the tool's own success line said so,
            # which the check above calls the authority. Honouring a cancel
            # from here would report FAILED for an update that completed, and
            # would skip the bridge exit and the LAN restore below, leaving the
            # device in USB-transparent mode with its LAN configuration down.
            # So the cancel is recorded as a note and the flow continues.
            #
            # A cancel observed BEFORE this point keeps the existing behaviour
            # exactly: recovery runs via _try_leave_lan_update_mode and the
            # update reports FAILED.
            if cancel_event is not None and cancel_event.is_set():
                self._logger.info(
                    "Cancellation was requested, but the WiFi module flash "
                    "had already completed successfully; continuing to "
                    "reconnect and restore LAN configuration. The cancel was "
                    "not honoured."
                )
            # Dropped for the remainder of the flow, deliberately: _reconnect
            # is the step that leaves the bridge and restores LAN, and it must
            # run to completion even with a cancel pending. This is the only
            # remaining consumer of cancel_event in this method.
            cancel_event = None

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

                # Leave the bridge first: while it is on, the firmware
                # forwards everything but this exact command to the WINC,
                # so the LAN restore below would never reach the console.
                self._send_and_settle(
                    device, scpi_commands.set_usb_transparency_mode(0),
                    self._options.post_usb_transparent_mode_exit_delay, deadline,
                )
                device.send(scpi_commands.enable_network_lan())
                # APPLY 1, not APPLY then SAVE: the firmware's apply gate
                # can refuse the apply (-200, a REINIT in flight), and a
                # separate SAVE would persist the unapplied settings anyway
                # (SCPI_LANSettingsSave copies them unconditionally). APPLY 1
                # saves only an accepted apply (SCPI_LANSettingsApply).
                device.send(scpi_commands.apply_and_save_network_lan())
                # Success ends with the same positive check recovery uses:
                # the console answers. The firmware only recognises the
                # exit when it arrives in one receive buffer, so a split
                # delivery would leave the bridge up and the LAN commands
                # forwarded to the WINC. No answer is a failure, so recovery
                # runs and the update isn't reported COMPLETE.
                for attempt in range(_SUCCESS_CONSOLE_CHECKS):
                    self._sleep_with_cancel(_BRIDGE_POLL_SECONDS, deadline)
                    if self._console_answers(device):
                        return
                raise IOError(
                    "The device's console did not answer after leaving the "
                    "WiFi bridge, so it may still be in USB-transparent mode."
                )

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
        except FirmwareUpdateCancelledError as exc:
            failed_state = self._current_state
            failed_operation = "WiFi module update canceled."
            if not self._try_leave_lan_update_mode(device, bridge_state):
                failed_operation += _NOT_RECOVERED
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
            # Captured first, so the failure names the step that failed, not
            # the recovery that follows.
            if not self._try_leave_lan_update_mode(device, bridge_state):
                failed_operation += _NOT_RECOVERED
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
        except BaseException:
            # KeyboardInterrupt / SystemExit, e.g. Ctrl-C while the flash
            # tool runs: the device can be bridged all the same, so it gets
            # the same recovery. The interrupt is re-raised unchanged; the
            # caller's finally sets FAILED even if a second interrupt lands
            # during this recovery.
            failed_operation = self._current_operation
            if not self._try_leave_lan_update_mode(device, bridge_state):
                failed_operation += _NOT_RECOVERED
            self._transition_to_state(FirmwareUpdateState.FAILED, failed_operation)
            raise

    # -- WiFi bridge (#190) ----------------------------------------------
    #
    # The firmware's WiFi manager (wifi_manager.c) has three states here, and
    # LAN:APPLY does something different in each:
    #   normal     - APPLY reinitialises WiFi;
    #   REQUESTED  - (FWUpdate received) APPLY ENTERS the bridge and turns
    #                USB-transparent mode on;
    #   READY      - APPLY leaves the bridge and clears both flags.
    # None of it can be queried, and the timing is open-ended: REINIT waits
    # out STA retry backoff (up to 30 s) before it looks at the flags, and
    # while one APPLY is in progress the apply gate refuses another with
    # -200. What can be observed: the console doesn't answer while
    # transparent mode is on, SetTransparentMode 0 always reaches it, and a
    # refused APPLY leaves -200 in the error queue.

    def _bridge_ask(self, device, *messages, completion_timeout_ms=200) -> List[str]:
        # Capped as a whole: a device still streaming text would otherwise
        # keep the exchange's inactivity timer from ever running out.
        return device.execute_text_command(
            lambda: [device.send(m) for m in messages],
            response_timeout_ms=1000,
            completion_timeout_ms=completion_timeout_ms,
            max_total_ms=_BRIDGE_EXCHANGE_MAX_MS,
        )

    def _read_error_code(self, device) -> Optional[int]:
        from daqifi._internal import scpi as _scpi
        from daqifi._internal import scpi_commands

        reply = _scpi.find_system_error_reply(
            self._bridge_ask(
                device,
                scpi_commands.get_last_error(),
                completion_timeout_ms=_APPLY_REPLY_WINDOW_MS,
            )
        )
        return None if reply is None else _scpi.parse_system_error(reply)[0]

    def _wifi_module_powered(self, device) -> Optional[bool]:
        """``SYSTem:POWer:STATe?``: 0 standby, 1 powered, 2 powered with
        external power down. LAN:APPLY only restarts WiFi, and so only
        enters or leaves the bridge, when powered (1 or 2:
        wifi_manager_UpdateNetworkSettings' ``isPowered``). ``None`` when
        the reply can't be read."""
        from daqifi._internal.comm import ScpiMessage

        for line in self._bridge_ask(device, ScpiMessage("SYSTem:POWer:STATe?")):
            stripped = line.strip()
            if stripped.upper().startswith("DAQIFI>"):
                stripped = stripped[len("DAQIFI>"):].strip()
            if stripped.lstrip("-").isdigit():
                return int(stripped) in (1, 2)
        return None

    def _console_answers(self, device) -> bool:
        from daqifi._internal.comm import ScpiMessage

        return any(line.strip() for line in self._bridge_ask(device, ScpiMessage("*IDN?")))

    def _send_apply(self, device) -> Optional[int]:
        """Drain the error queue, send LAN:APPLY and read the queue again.
        Returns the error code (0 = accepted, non-zero, e.g. -200 from the
        apply gate, = refused, so retry), or ``None`` when no reply was read:
        never acceptance. The bridge may be up, or the reply may be late."""
        from daqifi._internal import scpi as _scpi
        from daqifi._internal import scpi_commands

        # Drain stale entries first, so the code read after the APPLY is the
        # APPLY's: the firmware leaves -200s of its own in the queue during
        # the bridge transition.
        for _ in range(_ERROR_QUEUE_DRAIN_LIMIT):
            stale = _scpi.find_system_error_reply(
                self._bridge_ask(device, scpi_commands.get_last_error())
            )
            # No reply ends the drain too; the APPLY is still sent, and its
            # own reply decides.
            if stale is None or _scpi.parse_system_error(stale)[0] == 0:
                break
        # The -200 of a refused APPLY can trail its echo by most of a
        # second, so the read waits longer than the other exchanges.
        reply = _scpi.find_system_error_reply(
            self._bridge_ask(
                device,
                scpi_commands.apply_network_lan(),
                scpi_commands.get_last_error(),
                completion_timeout_ms=_APPLY_REPLY_WINDOW_MS,
            )
        )
        return None if reply is None else _scpi.parse_system_error(reply)[0]

    def _enter_wifi_bridge(self, device, deadline: _Cancellation) -> None:
        """Send LAN:APPLY until it is accepted, then wait until the console
        stops answering, i.e. the bridge is up, before the flash tool runs.

        Success is only ever the console going silent; no APPLY reply is
        taken as proof. With no reply read, a console that still answers is
        asked for the error queue again, so a late -200 means retry, and a
        0 means accepted, so wait. Raises TimeoutError at ``deadline``,
        which covers the longest REINIT deferral plus the bridge entry.
        """
        accepted = False
        while not accepted:
            code = self._send_apply(device)
            if code is None:
                if not self._console_answers(device):
                    return
                code = self._read_error_code(device)
            if code == 0:
                accepted = True
            else:
                self._sleep_with_cancel(_BRIDGE_POLL_SECONDS, deadline)
        while self._console_answers(device):
            self._sleep_with_cancel(_BRIDGE_POLL_SECONDS, deadline)

    def _try_leave_lan_update_mode(
        self, device: "DaqifiStreamingDevice", bridge_state: dict
    ) -> bool:
        """Best effort: return the device to normal after a failed or
        cancelled WiFi update. Never raises; returns whether the device was
        confirmed back to normal.

        Once FWUpdate has been attempted (a send that raised may still have
        been delivered) the device can be in any of the three states, and a
        deferred REINIT can still change it later. So:

        1. reconnect, then ``SetTransparentMode 0`` + APPLY until the error
           queue shows the APPLY was accepted;
        2. watch the console for ``WIFI_BRIDGE_SETTLE_SECONDS``: the longest
           REINIT deferral plus the bridge entry. It's a firmware bound, not
           a preference. If the console goes silent, the bridge came up, so
           go back to 1, where that APPLY now leaves it;
        3. a full watch with the console answering is confirmation. Reaching
           ``wifi_recovery_timeout`` first, at any step, means unconfirmed.

        Nothing is saved after a flash that didn't complete. Ignores the
        caller's cancel, which is already set when this runs after a cancel.
        """
        bridge_state["recovered"] = None
        if not bridge_state.get("update_requested"):
            bridge_state["recovered"] = True
            return True
        from daqifi._internal import scpi_commands

        budget = self._options.wifi_recovery_timeout
        deadline = _Cancellation(
            FirmwareUpdateState.VERIFYING,
            "leave WiFi update mode",
            budget,
            None,
            lambda seconds: (
                f"Device not confirmed out of WiFi update mode within "
                f"{seconds:.0f} s."
            ),
        )
        exit_delay = self._options.post_usb_transparent_mode_exit_delay
        try:
            self._wait_for_serial_reconnect(device, deadline)
            # In standby APPLY doesn't restart WiFi, so it can't consume a
            # pending update request, and a quiet console proves nothing:
            # only a positive "powered" lets recovery go on. No answer at all
            # can mean the bridge came up a moment ago, so it is retried
            # behind a fresh exit; the budget running out (the deadline
            # raises) is what makes it unconfirmed.
            while True:
                self._send_and_settle(
                    device, scpi_commands.set_usb_transparency_mode(0),
                    exit_delay, deadline,
                )
                powered = self._wifi_module_powered(device)
                if powered is True:
                    break
                if powered is False:
                    raise RuntimeError(
                        "the device is in standby, so LAN:APPLY doesn't "
                        "restart WiFi and a pending update request can't be "
                        "cleared"
                    )
                self._sleep_with_cancel(_BRIDGE_POLL_SECONDS, deadline)
            while True:
                # The bridge can come up between attempts, so every APPLY
                # is preceded by the exit that gets it to the console.
                while True:
                    self._send_and_settle(
                        device, scpi_commands.set_usb_transparency_mode(0),
                        exit_delay, deadline,
                    )
                    if self._send_apply(device) == 0:
                        break
                    self._sleep_with_cancel(_BRIDGE_POLL_SECONDS, deadline)
                watch_end = time.monotonic() + WIFI_BRIDGE_SETTLE_SECONDS
                bridged = False
                while time.monotonic() < watch_end:
                    self._sleep_with_cancel(_BRIDGE_POLL_SECONDS, deadline)
                    if not self._console_answers(device):
                        bridged = True
                        break
                if not bridged:
                    self._logger.info(
                        "The device is back to normal after a failed WiFi "
                        "module update."
                    )
                    bridge_state["recovered"] = True
                    return True
        except Exception as exc:
            self._logger.warning(
                "Could not confirm the device left WiFi update mode after a "
                "failed update; it may still be in USB-transparent bridge "
                "mode, or hold a pending update request, and need a power "
                "cycle: %s",
                exc,
            )
            bridge_state["recovered"] = False
            return False

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
        firmware_version: Optional[str] = None,
    ) -> ExternalProcessRequest:
        # Absolute before anything else: the tool runs with its own directory
        # as the working directory, so a relative tool or firmware path would
        # be resolved against that directory instead of the caller's.
        firmware_path = _anchor_to_cwd(firmware_path)
        tool_path = _anchor_to_cwd(self._resolve_wifi_tool_path(firmware_path))
        port = self._resolve_wifi_port(device)

        template = self._options.wifi_flash_tool_arguments_template
        if _VERSION_PLACEHOLDER in template and firmware_version is None:
            # _plan_wifi_update settles the version at entry; this is the
            # backstop for a path that reaches here without it.
            raise ValueError(
                f"The WiFi flash arguments template {template!r} passes "
                "{version}, but no firmware version was given."
            )

        executable_path = tool_path
        executable_arguments: Any
        extension = os.path.splitext(tool_path)[1].lower()
        # The template is split by the rules of the program that will parse
        # it, then each value goes into its argument whole, so nothing
        # substituted is parsed again:
        #   .cmd/.bat (any host)        cmd.exe rules, see _cmd_exe_arguments
        #   Windows program (native, or .exe/.com under WSL)
        #                               C-runtime rules; passed as a list,
        #                               which list2cmdline (native) or WSL
        #                               turns back into a C-runtime line
        #   POSIX program               shlex; passed as a list (argv)
        if extension in (".exe", ".com") or (
            _is_native_windows() and extension not in (".cmd", ".bat")
        ):
            executable_arguments = _substitute_placeholders(
                _split_crt_template(template),
                port,
                _path_for_windows_tool(firmware_path),
                firmware_version,
            )
        elif extension not in (".cmd", ".bat"):
            executable_arguments = _substitute_placeholders(
                _split_posix_template(template), port, firmware_path,
                firmware_version,
            )
        else:
            cmd_exe = shutil.which("cmd.exe")
            if cmd_exe is None:
                raise FileNotFoundError(
                    f"WiFi flash tool {tool_path!r} is a Windows .cmd/.bat "
                    "script but cmd.exe was not found on PATH. Run from "
                    "Windows or WSL with Windows interop enabled."
                )
            executable_path = cmd_exe
            # The script is named relative to the working directory set
            # below (WSL translates that for the Windows process), and Linux
            # paths in its arguments are translated: WSL hands arguments to
            # Windows programs as-is.
            if _path_for_windows_tool(os.path.dirname(tool_path)).startswith("\\\\"):
                # cmd.exe refuses a UNC path (\\server\share, or the
                # \\wsl.localhost\... path WSL gives a Linux-filesystem
                # directory) as its working directory, so the script could
                # not be found once the device is already in update mode.
                raise FileNotFoundError(
                    f"WiFi flash tool {tool_path!r} is on a network or Linux "
                    "filesystem path, which cmd.exe cannot use as its working "
                    "directory. Put the WiFi firmware bundle on a local Windows "
                    "drive (under WSL, a path on the mounted Windows drive)."
                )
            executable_arguments = _cmd_exe_arguments(
                os.path.basename(tool_path),
                template,
                port,
                _path_for_windows_tool(firmware_path),
                native_windows=_is_native_windows(),
                version=firmware_version,
            )

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

    def _plan_wifi_update(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: _PROGRESS_CALLBACK,
        firmware_version: Optional[str],
    ) -> "_WifiPlan":
        """Settle, before anything reaches the device, what a template that
        passes ``{version}`` needs: the tool reports a version it cannot use
        only at the very end, as "Firmware release not found", after the
        device is in WiFi-update mode (#270).

        Raises ValueError (_WifiRequestRefused) for a refusal. Any other
        failure to find the tool or build the request (no cmd.exe, a UNC
        bundle, no serial port) is held in the plan and raised where building
        always failed, in PREPARING_DEVICE after the device-connected check.
        """
        if firmware_version is not None and (
            not isinstance(firmware_version, str)
            or not firmware_version.strip()
        ):
            raise _WifiRequestRefused(
                "firmware_version must be a non-empty string, the version "
                "download_wifi_firmware returns with the path (got "
                f"{firmware_version!r}). Nothing was sent."
            )
        settings = self._wifi_tool_settings()
        if _VERSION_PLACEHOLDER not in settings[1]:
            return _WifiPlan(version=firmware_version, settings=settings)
        try:
            tool_path = _anchor_to_cwd(
                self._resolve_wifi_tool_path(_anchor_to_cwd(firmware_path))
            )
            if _is_default_wifi_setup(tool_path, settings[1]):
                return _WifiPlan(
                    request=self._build_default_wifi_request(
                        device, firmware_path, progress, firmware_version,
                        tool_path,
                    ),
                    settings=settings,
                )
            if firmware_version is None:
                firmware_version = _single_bundle_version(tool_path)
            return _WifiPlan(version=firmware_version, settings=settings)
        except _WifiRequestRefused:
            raise
        except Exception as exc:
            return _WifiPlan(error=exc, settings=settings)

    def _build_default_wifi_request(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: _PROGRESS_CALLBACK,
        firmware_version: Optional[str],
        tool_path: str,
    ) -> ExternalProcessRequest:
        """The default tool and arguments: check the values substituted into
        ``/p {port} ... /v {version}`` and the image the tool will open, then
        build the request that will run. Only the values can vary, so
        allow-listing them is what keeps the rendered arguments as written:
        cmd.exe splits a batch script's arguments at , ; = as well as
        whitespace, and winc_flash_tool.cmd enables delayed expansion."""
        device_directory = os.path.join(
            os.path.dirname(tool_path), "files", "winc1500"
        )
        if firmware_version is None:
            available = _wifi_bundle_versions(device_directory)
            if len(available) != 1:
                found = (
                    f"it holds {', '.join(available)}" if available
                    else "it holds no <version>/firmware/programmer_firmware.bin"
                )
                raise _WifiRequestRefused(
                    "The WiFi firmware version could not be read from "
                    f"{device_directory!r}: {found}. Pass firmware_version=, "
                    "the version download_wifi_firmware returns with the "
                    "path. Nothing was sent."
                )
            firmware_version = available[0]
        if not _WINC_VERSION_ALLOWED.fullmatch(firmware_version):
            raise _WifiRequestRefused(
                f"WiFi firmware version {firmware_version!r} may contain only "
                "letters, digits, '.', '_' and '-'. Nothing was sent."
            )
        image = os.path.join(
            device_directory, firmware_version, "firmware",
            "programmer_firmware.bin",
        )
        if not os.path.isfile(image):
            raise _WifiRequestRefused(
                f"WiFi firmware version {firmware_version!r} needs {image!r}, "
                "which does not exist; the flash tool would fail with "
                "'Firmware release not found'. Nothing was sent."
            )
        port = self._resolve_wifi_port(device)
        if not _WINC_PORT_ALLOWED.fullmatch(port):
            raise _WifiRequestRefused(
                f"WiFi flash tool port {port!r} may contain only letters, "
                "digits, '.', '_', ':', '/' and '-'. Nothing was sent."
            )
        return self._build_wifi_process_request(
            device, firmware_path, progress, firmware_version
        )

    def _wifi_tool_settings(self) -> Tuple[str, str]:
        return (
            self._options.wifi_flash_tool_file_name,
            self._options.wifi_flash_tool_arguments_template,
        )

    def _wifi_request_for(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: _PROGRESS_CALLBACK,
        plan: "_WifiPlan",
    ) -> ExternalProcessRequest:
        if plan.error is not None:
            raise plan.error
        if plan.request is not None:
            # The request checked at entry, never a second render of it.
            return plan.request
        if (
            _VERSION_PLACEHOLDER in self._options.wifi_flash_tool_arguments_template
            and plan.settings is not None
            and self._wifi_tool_settings() != plan.settings
        ):
            # Options are mutable: what _plan_wifi_update settled no longer
            # describes them, and a {version} template must not be rendered
            # unsettled -- possibly as the default, unchecked.
            raise RuntimeError(
                "The WiFi flash tool options changed during the update; the "
                "version and arguments were settled at its start for the old "
                "ones."
            )
        return self._build_wifi_process_request(
            device, firmware_path, progress, plan.version
        )

    def _resolve_wifi_tool_path(self, firmware_path: str) -> str:
        if os.path.isfile(firmware_path):
            return firmware_path

        if os.path.isdir(firmware_path):
            tool_name = self._options.wifi_flash_tool_file_name
            matches: List[str] = []
            # Two safeguards so the walk runs in bounded time before the
            # PROGRAMMING state-timeout wrapper is even entered:
            #   1. Short-circuit at 2 matches (we only need to distinguish
            #      "0" / "exactly 1" / ">1" cases).
            #   2. Hard cap on traversed dirs/files so a missing tool
            #      under a huge tree fails fast instead of hanging
            #      _build_wifi_process_request indefinitely.
            scanned_dirs = 0
            scanned_files = 0
            for root, _dirs, files in os.walk(firmware_path):
                scanned_dirs += 1
                if scanned_dirs > _WIFI_TOOL_SCAN_MAX_DIRS:
                    raise FileNotFoundError(
                        f"Search for {tool_name!r} under {firmware_path!r} "
                        f"exceeded {_WIFI_TOOL_SCAN_MAX_DIRS} directories; "
                        "please pass an explicit tool path."
                    )
                for name in files:
                    scanned_files += 1
                    if scanned_files > _WIFI_TOOL_SCAN_MAX_FILES:
                        raise FileNotFoundError(
                            f"Search for {tool_name!r} under "
                            f"{firmware_path!r} exceeded "
                            f"{_WIFI_TOOL_SCAN_MAX_FILES} files; please "
                            "pass an explicit tool path."
                        )
                    if name == tool_name:
                        matches.append(os.path.join(root, name))
                        if len(matches) >= 2:
                            break
                if len(matches) >= 2:
                    break

            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                # Multiple firmware versions extracted into the same
                # tree (e.g. WiFi 19.5.4 + 19.6.1 side-by-side) — refuse
                # to pick one implicitly so we never flash the wrong
                # bundle. Caller should pass the explicit tool path.
                raise FileNotFoundError(
                    f"Multiple {tool_name!r} tools found under "
                    f"{firmware_path!r}; please pass an explicit tool "
                    f"path. Matches: {matches!r}"
                )

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

        # The serial port the device is connected through, not its display
        # name: DaqifiDeviceFactory names every device "DaqifiDevice" by
        # default, which the flash tool would be handed as its port.
        transport = getattr(device, "transport", None)
        port_name = getattr(transport, "port_name", None)
        if port_name and str(port_name).strip():
            return str(port_name)
        if transport is not None:
            raise RuntimeError(
                "The device is not connected over a serial port, so the WiFi "
                "flash tool's port is unknown; set "
                "FirmwareUpdateServiceOptions.wifi_port_override."
            )

        # Duck-typed devices with no transport keep the old contract: their
        # name is their port.
        name = getattr(device, "name", None)
        if name and str(name).strip():
            return str(name)

        raise RuntimeError(
            "Unable to resolve a serial port name for WiFi update."
        )

    # -- bootloader I/O --------------------------------------------------

    def _parse_hex_records(
        self, hex_file_path: str
    ) -> Tuple[List[bytes], Optional[List[FlashCrcRegion]]]:
        """The image's records to program, and the flash regions to verify
        by CRC afterwards (None when the protocol can't read a CRC back)."""
        with open(hex_file_path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        protocol = self._bootloader_protocol
        records = list(protocol.parse_hex_file(lines))
        regions: Optional[List[FlashCrcRegion]] = None
        if all(
            callable(getattr(protocol, name, None))
            for name in (
                "compute_crc_regions",
                "create_read_crc_message",
                "decode_read_crc_response",
            )
        ):
            regions = list(protocol.compute_crc_regions(lines))
        return records, regions

    def _bootloader_enumerator(self) -> "HidDeviceEnumerator":
        from daqifi._internal.hid_enumerator import HidLibraryDeviceEnumerator

        if self._hid_device_enumerator is None:
            self._hid_device_enumerator = HidLibraryDeviceEnumerator()
        return self._hid_device_enumerator

    @staticmethod
    def _bootloader_identity(
        device: "HidDeviceInfo", location: Optional[str]
    ) -> Tuple[Any, Any, Any]:
        # The USB location is part of the identity because Linux reuses
        # hidraw node names: a pre-existing board that unplugs can free
        # /dev/hidraw0 for ours, and path alone would then filter ours out.
        return (
            getattr(device, "device_path", None),
            getattr(device, "serial_number", None),
            location,
        )

    def _snapshot_bootloader_devices(
        self, deadline: _Cancellation
    ) -> FrozenSet[Tuple[Any, Any, Any]]:
        """Identities of the bootloaders enumerated before FORceBoot.

        Retries transient enumeration errors until the deadline, then
        raises: without the snapshot, a board that was already sitting in
        its bootloader is indistinguishable from the one being updated.
        Nothing has been sent to the device yet, so failing here is safe.
        """
        enumerator = self._bootloader_enumerator()
        while True:
            deadline.check()
            try:
                devices = enumerator.enumerate(
                    self._options.bootloader_vendor_id,
                    self._options.bootloader_product_id,
                )
                return frozenset(
                    self._bootloader_identity(
                        d, hid_device_location(getattr(d, "device_path", None))
                    )
                    for d in devices
                )
            except Exception as exc:
                self._last_bootloader_enumeration_error = exc
                self._logger.debug(
                    "HID enumeration failed before FORceBoot; will retry "
                    "until timeout: %s",
                    exc,
                )
                self._sleep_with_cancel(self._options.poll_interval, deadline)

    def _list_application_ports(self, deadline: _Cancellation) -> Set[str]:
        """Serial ports of DAQiFi boards in their application.

        Retried briefly, then refused: a listing that failed can't show that
        only one board is attached.
        """
        for attempt in range(3):
            deadline.check()
            ports = _list_usb_serial_ports(
                self._options.application_vendor_id,
                self._options.application_product_id,
            )
            if ports is not None:
                return set(ports)
            if attempt < 2:
                self._sleep_with_cancel(self._options.poll_interval, deadline)
        raise RuntimeError(
            "Could not list serial ports to check that only one DAQiFi board "
            "is connected; refusing to start the update. Check that pyserial "
            "can enumerate ports on this host, then retry."
        )

    def _refuse_if_several_boards_attached(self, applications: Set[str]) -> None:
        """Refuse to start while more than one DAQiFi board is on USB.

        Where the bootloader can't be bound to the board's USB port (Windows,
        macOS, WSL2 + usbipd), another board entering its bootloader during
        the wait looks the same as ours, so an update only starts with one
        board attached. Nothing has been sent to the device yet. A board
        plugged in, or still re-enumerating, at the time of this check is
        not caught (see the README). A board seen both as an application
        port and as a bootloader counts twice, which only errs towards
        refusing.
        """
        bootloaders = len(self._preexisting_bootloaders)
        if len(applications) + bootloaders <= 1:
            return
        found = sorted(applications)
        if bootloaders:
            found.append(f"{bootloaders} in bootloader mode")
        raise RuntimeError(
            f"{len(applications) + bootloaders} DAQiFi boards are connected "
            f"over USB ({', '.join(found)}; a USB serial port whose IDs can't "
            "be read counts as one). A PIC32 update needs exactly one: "
            "with several attached, another board's bootloader could be "
            "flashed instead. Disconnect every board except the one being "
            "updated, including any left in bootloader mode, then retry."
        )

    def _raise_if_target_returned_to_application(self) -> None:
        """Stop when the device rebooted back into its application.

        A board without the bootloader treats FORceBoot as a plain reboot:
        its serial port drops and comes back, and no bootloader of its own
        ever appears. Any "new" bootloader seen after that belongs to some
        other board, so the wait must not go on picking one. Requires the
        port to have been seen gone first, so the window before the reset
        lands is not mistaken for a return.
        """
        if not self._target_port_name:
            return
        ports = _list_serial_port_names()
        if ports is None:
            return
        # Re-resolved each poll: a by-id alias dangles while the device is
        # gone and points at its node again once it is back.
        present = canonical_port_name(self._target_port_name) in ports
        if not present:
            self._target_port_seen_gone = True
        elif self._target_port_seen_gone:
            if (
                self._target_usb_location is not None
                and serial_port_location(self._target_port_name)
                != self._target_usb_location
            ):
                # Linux reuses ttyACM names: the node that came back belongs
                # to a board on another port, not to ours.
                return
            raise RuntimeError(
                f"{self._target_port_name} came back as the application after "
                "FORceBoot instead of entering the bootloader; this device "
                "does not appear to carry the PIC32 bootloader. Refusing to "
                "select any other bootloader device."
            )

    def _wait_for_bootloader_device(
        self, deadline: _Cancellation
    ) -> "HidDeviceInfo":
        enumerator = self._bootloader_enumerator()

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

            # Each candidate's USB location is read once per poll and that one
            # reading is used throughout: re-reading it later could see a
            # different board that has since taken over the same node.
            located = [
                (d, hid_device_location(getattr(d, "device_path", None)))
                for d in devices
            ]

            # Only a bootloader that appeared after FORceBoot can be the
            # device being updated. One that was already enumerated belongs
            # to another board, and taking it when it happens to be the only
            # candidate (ours still re-enumerating) would erase that board.
            located = [
                (d, location)
                for d, location in located
                if self._bootloader_identity(d, location)
                not in self._preexisting_bootloaders
            ]
            self._raise_if_target_returned_to_application()

            # A new bootloader can still be another board that entered its
            # bootloader during the wait. Where both locations are known
            # (Linux, not usbip) the bootloader must sit on the USB port this
            # device's serial port used. Where either is unknown, only the
            # snapshot above protects the selection.
            if self._target_usb_location is not None:
                # A positive match only: a candidate whose location can't be
                # read (e.g. a board attached over usbip) is not shown to be
                # on our port, so it is not ours to erase.
                on_our_port = [
                    (d, location)
                    for d, location in located
                    if location == self._target_usb_location
                ]
                self._rejected_by_location = len(located) - len(on_our_port)
                located = on_our_port
            devices_list = [d for d, _ in located]
            if len(located) == 1:
                self._selected_bootloader_location = located[0][1]
                return located[0][0]
            if len(devices_list) > 1:
                # Multiple bootloader-mode units appeared on the same
                # workstation — refuse to pick one implicitly. The
                # caller should narrow the field (e.g. detach all but
                # the target unit via usbipd, or extend this Service to
                # accept an expected serial filter) so we never flash
                # the wrong device.
                details = ", ".join(
                    f"{getattr(d, 'serial_number', None)!r}:"
                    f"{getattr(d, 'device_path', None)!r}"
                    for d in devices_list
                )
                raise RuntimeError(
                    "Multiple HID bootloader devices detected; refusing "
                    f"to pick one implicitly. Candidates: {details}"
                )

            self._sleep_with_cancel(self._options.poll_interval, deadline)

    def _connect_to_bootloader_with_retry(
        self,
        bootloader_device: "HidDeviceInfo",
        deadline: _Cancellation,
    ) -> None:
        def _attempt() -> None:
            if self._hid_transport.is_connected:
                self._hid_transport.disconnect()
            # By path: bootloaders report no serial string, so connecting by
            # VID/PID/serial alone could open a different board than the
            # one selected above.
            self._hid_transport.connect(
                self._options.bootloader_vendor_id,
                self._options.bootloader_product_id,
                bootloader_device.serial_number,
                device_path=bootloader_device.device_path,
            )
            # Checked after the open, against the device now behind the
            # handle: if ours vanished between selection and open, Linux can
            # hand its hidraw node to another board, and the path alone would
            # open that board. A node swapped after the open leaves this
            # handle on the departed device, where I/O fails.
            expected = self._selected_bootloader_location
            if expected is not None:
                actual = hid_device_location(bootloader_device.device_path)
                if actual != expected:
                    self._hid_transport.disconnect()
                    raise _WrongBootloaderError(
                        f"{bootloader_device.device_path!r} moved from USB port "
                        f"{expected} to {actual} between selection and open; "
                        "refusing to program a different board."
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

        attempts = [0]

        def _attempt() -> None:
            attempts[0] += 1
            if attempts[0] > 1:
                # The last attempt's reply may still arrive; a retry must not
                # take it as its own (acks carry only the command byte).
                self._drain_hid_reports()
            self._flash_touched = True
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

            attempts = [0]

            def _attempt(record=record, index=index, attempts=attempts) -> None:
                attempts[0] += 1
                if attempts[0] > 1:
                    # As for the erase: a late ack to the failed attempt would
                    # otherwise be read as this one's and shift every later
                    # record's ack by one.
                    self._drain_hid_reports()
                message = self._bootloader_protocol.create_program_flash_message(
                    record
                )
                self._flash_touched = True
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

    def _verify_flash(
        self,
        crc_regions: Optional[Sequence[FlashCrcRegion]],
        deadline: _Cancellation,
    ) -> None:
        """Check every programmed region by asking the bootloader for its
        CRC (READ_CRC) and comparing with the image's. Port of daqifi-core
        2826b35.

        A reply carries no address (datastream.c answers READ_CRC with the
        command byte and the CRC only), so a late reply to one request would
        read as the answer to the next, and a late reply to an earlier
        command (a PROGRAM ack) can arrive at any point. Pending reports are
        drained before every request, and nothing is retried: a reply that
        isn't a well-formed READ_CRC response, or no reply at all, leaves the
        flash unconfirmed and fails the check, as a CRC that differs does.
        Without regions (a protocol that can't read a CRC back) only the
        bootloader's liveness is checked, as before.
        """
        from datetime import timedelta

        if not crc_regions:
            version = self._request_bootloader_version(deadline)
            self._logger.info(
                "No flash CRC readback available; bootloader still answers: %s",
                version,
            )
            return

        for index, region in enumerate(crc_regions):
            deadline.check()
            where = f"region {index + 1} at 0x{region.address:08X}"

            actual = self._read_region_crc(region, where)
            if actual != region.expected_crc:
                raise _FlashVerificationError(
                    f"Flash CRC mismatch in {where} (length {region.length}): "
                    f"expected 0x{region.expected_crc:04X}, bootloader "
                    f"reported 0x{actual:04X}."
                )
        self._logger.info("Flash verified: %d region(s) match the image.", len(crc_regions))

    def _read_region_crc(self, region: FlashCrcRegion, where: str) -> int:
        """One READ_CRC exchange for ``region``, after draining stale
        reports; never retried. No reply, or a reply that isn't exactly a
        READ_CRC response, raises _FlashVerificationError: the CRC is
        unknown."""
        from datetime import timedelta

        protocol = self._bootloader_protocol
        self._drain_hid_reports()
        self._hid_transport.write(
            protocol.create_read_crc_message(region.address, region.length)
        )
        try:
            response = self._hid_transport.read(
                timedelta(seconds=self._options.bootloader_response_timeout)
            )
        except TimeoutError:
            response = b""
        if not response:
            raise _FlashVerificationError(
                f"The bootloader didn't answer the CRC request for {where}; "
                "the flash can't be confirmed."
            )
        actual = protocol.decode_read_crc_response(response)
        if actual is None:
            raise _FlashVerificationError(
                f"The reply to the CRC request for {where} wasn't a CRC reply "
                "(a late reply to an earlier command, or a damaged frame); "
                "the flash can't be confirmed."
            )
        return actual

    def _confirm_flash_erased(self, deadline: _Cancellation) -> None:
        """Read back, after the cleanup erase, the reset vector and every
        programmed region, and require the CRC of erased (0xFF) flash. The
        ERASE ack proves nothing: APP_NVMOperation (nvm.c) checks no NVM
        error flag, and BOOTLOADER_WAIT_FOR_NVM acks once the write cycle
        completes. The reset vector decides whether a stale application
        starts (app.c), and a sparse image's regions needn't cover it."""
        reset_vector = getattr(self._bootloader_protocol, "reset_vector_regions", None)
        regions = self._crc_regions
        if not regions or not callable(reset_vector):
            raise _FlashVerificationError(
                "The re-erase can't be confirmed: this bootloader protocol "
                "can't read the flash back."
            )
        checks = [
            (f"the reset vector at 0x{region.address:08X}", region)
            for region in reset_vector()
        ] + [
            (f"region {index + 1} at 0x{region.address:08X}", region)
            for index, region in enumerate(regions)
        ]
        for where, region in checks:
            deadline.check()
            blank = Crc16(b"\xff" * region.length).crc
            actual = self._read_region_crc(region, where)
            if actual != blank:
                raise _FlashVerificationError(
                    f"After the re-erase {where} isn't blank: CRC 0x{actual:04X}, "
                    f"erased flash gives 0x{blank:04X}."
                )

    def _drain_hid_reports(self, limit: int = 16) -> None:
        """Discard reports already waiting from the bootloader (a late reply
        to an earlier request). A 1 ms poll: it only clears what has already
        arrived, so it stays cheap before each of many CRC requests; a reply
        later still is caught by the strict decode. A read that times out
        (``b""``, or TimeoutError from some transports) means nothing is
        waiting."""
        from datetime import timedelta

        for _ in range(limit):
            try:
                if not self._hid_transport.read(timedelta(milliseconds=1)):
                    return
            except TimeoutError:
                return

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

        # Application-readiness probe (port of daqifi-core PR #200,
        # closes #145 upstream). Serial transport re-enumeration succeeds
        # well before the PIC32 application firmware can answer protobuf
        # status queries — the probe gives downstream flows (LAN chip
        # info, WiFi prep) a chance to wait for true application
        # readiness instead of reimplementing their own retry. Opt-in via
        # ``options.post_reconnect_readiness_probe``; when None, legacy
        # "serial reopened == done" semantics apply.
        probe = self._options.post_reconnect_readiness_probe
        if probe is not None:
            self._wait_for_application_ready(device, probe, deadline)

    def _wait_for_application_ready(
        self,
        device: "DaqifiStreamingDevice",
        probe: Callable[["DaqifiStreamingDevice", _Cancellation], bool],
        outer_deadline: _Cancellation,
    ) -> None:
        """Block until ``probe`` returns True or the readiness budget elapses.

        Mirrors upstream ``WaitForApplicationReadyAsync`` from
        ``Daqifi.Core/Firmware/FirmwareUpdateService.cs`` (PR #200).
        Probe exceptions are treated as "not ready, retry" so the
        update loop survives transient probe failures; the most recent
        exception is re-raised as the cause when the budget elapses
        without success.
        """
        total_timeout = self._options.post_reconnect_readiness_timeout
        retry_delay = self._options.post_reconnect_readiness_retry_delay

        # Surface the wait at INFO so observers tailing the log can
        # distinguish "stuck" from "deliberately polling". The wait can
        # take up to ``post_reconnect_readiness_timeout`` (default 30s);
        # without this, the JUMPING_TO_APP state appears hung beyond
        # the initial transport reopen.
        self._logger.info(
            "Waiting up to %.3fs for device to become application-ready "
            "(post-reconnect readiness probe).",
            total_timeout,
        )
        wait_start = time.monotonic()

        # Track the readiness budget locally; the outer deadline is checked
        # via ``outer_deadline.check()`` each iteration so both gates fire.
        readiness_deadline_at = wait_start + total_timeout

        # Capture the most recent probe-thrown exception so the eventual
        # TimeoutError can carry the underlying cause as ``__cause__``.
        # Without this, deterministic probe failures (e.g. transport
        # says it's open but the device never responds to status
        # queries) report only as "timed out" — losing the actual error
        # context unless DEBUG logs are on.
        last_probe_exception: Optional[BaseException] = None

        # Tracks how many probe invocations have actually run. Distinct
        # from any loop iteration counter so the timeout messages don't
        # claim "attempt N" when the budget fired before a probe ever
        # executed.
        probes_executed = 0

        while True:
            outer_deadline.check()

            readiness_remaining = readiness_deadline_at - time.monotonic()
            if readiness_remaining <= 0:
                msg = (
                    f"Device did not become application-ready within "
                    f"{total_timeout:.3f}s (probes executed: "
                    f"{probes_executed}). The transport reconnected but "
                    "the readiness probe never returned true; the device "
                    "may still be initializing or the firmware may have "
                    "failed to start."
                )
                raise TimeoutError(msg) from last_probe_exception

            # Build a readiness-scoped deadline for the probe whose budget
            # is the AND of the outer cancel and the readiness budget —
            # whichever fires first ends the wait. Without this, a
            # well-behaved probe that uses ``deadline.remaining`` to bound
            # its I/O would honor the outer JUMPING_TO_APP timeout (often
            # 45s) instead of the readiness budget (default 30s), masking
            # the readiness-specific timeout entirely. Mirrors upstream
            # PR #200's ``CreateLinkedTokenSource(cancel, timeoutCts)``
            # pattern. (Qodo /agentic_review pass on PR #139 caught this.)
            probe_budget = min(outer_deadline.remaining, readiness_remaining)
            probe_deadline = _Cancellation(
                state=outer_deadline.state,
                operation="post-reconnect readiness probe",
                timeout_seconds=probe_budget,
                cancel_event=outer_deadline._cancel_event,
                timeout_message_builder=lambda secs: (
                    f"post-reconnect readiness probe deadline {secs:.3f}s elapsed"
                ),
            )

            try:
                probes_executed += 1
                is_ready = probe(device, probe_deadline)

                # Successful probe invocation (True OR False) means the
                # most recent attempt completed normally. Clear
                # last_probe_exception so a later timeout doesn't carry
                # forward a stale exception from an earlier failed
                # attempt.
                last_probe_exception = None

                if is_ready:
                    elapsed = time.monotonic() - wait_start
                    self._logger.info(
                        "Device became application-ready after %.3fs on "
                        "probe attempt %d.",
                        elapsed,
                        probes_executed,
                    )
                    return

                self._logger.debug(
                    "Application-ready probe returned false on attempt %d; "
                    "will retry.",
                    probes_executed,
                )
            except FirmwareUpdateCancelledError:
                # Outer cancellation propagated through the probe — bail
                # immediately, do NOT swallow as a probe failure.
                raise
            except Exception as exc:
                # Any other exception from the probe is treated as
                # "not ready, retry" so the update loop survives
                # transient probe failures (e.g. probe queries time out
                # because the firmware hasn't booted yet).
                # Catching ``Exception`` (not ``BaseException``) lets
                # ``KeyboardInterrupt`` / ``SystemExit`` / ``GeneratorExit``
                # propagate normally so the process can terminate.
                # ``FirmwareUpdateCancelledError`` is ``Exception``-derived
                # but is caught above this clause, so the ordering preserves
                # cooperative-cancel semantics. (Qodo /agentic_review on
                # PR #139 caught the original ``BaseException`` form.)
                last_probe_exception = exc
                self._logger.debug(
                    "Application-ready probe threw on attempt %d "
                    "(%s); treating as not-ready and retrying.",
                    probes_executed,
                    exc,
                )

            # Sleep until the next attempt, but never beyond the
            # readiness deadline. Partial-sleep keeps the timeout
            # message's "probes executed: N" count honest.
            remaining = readiness_deadline_at - time.monotonic()
            if remaining <= 0:
                continue  # next loop iteration raises the TimeoutError
            self._sleep_with_cancel(min(retry_delay, remaining), outer_deadline)

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
        if max_attempts < 1:
            # `range(1, 1)` would silently no-op the operation entirely.
            # FirmwareUpdateServiceOptions.validate() catches the common
            # case (hid_connect_retry_count / flash_write_retry_count
            # >= 1), but reject any direct miscall here too so a future
            # caller doesn't accidentally skip a destructive write.
            raise ValueError(
                f"Retry policy for {operation!r} must allow at least 1 "
                f"attempt (got {max_attempts})."
            )
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
        if self._rejected_by_location:
            details += (
                f" Ignored {self._rejected_by_location} bootloader device(s) "
                "not shown to be on this device's USB port "
                f"({self._target_usb_location})."
            )
        if self._preexisting_bootloaders:
            details += (
                f" Ignored {len(self._preexisting_bootloaders)} bootloader "
                "device(s) already enumerated before FORceBoot (another "
                "board); detach it to update this one."
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
            FirmwareUpdateState.RECOVERED,
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
        cleanup_outcome: Optional[str] = None,
    ) -> FirmwareUpdateException:
        if isinstance(exception, FirmwareUpdateException):
            return exception
        # When a re-erase ran, its outcome is what the user must act on.
        recovery_guidance = _CLEANUP_GUIDANCE.get(
            cleanup_outcome, None
        ) or _build_recovery_guidance(failed_state)
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


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    """Raise the internal cancellation sentinel if ``cancel_event`` is set.

    Used at flow-entry points that run BEFORE any state-timeout
    deadline is established (e.g. the WiFi version-check round-trip),
    so a pre-cancelled run aborts before incurring side effects.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise FirmwareUpdateCancelledError()


def _clamp_process_timeout(
    request: ExternalProcessRequest, remaining_seconds: float
) -> ExternalProcessRequest:
    """Return a copy of ``request`` whose timeout is min(its own, remaining).

    Raises ``TimeoutError`` if the state deadline has already expired —
    no point launching the subprocess just to have it TimeoutExpired
    immediately, and the empty-budget case otherwise rounds to a 0s
    timeout that some runners treat as nondeterministic.

    Reserves a small cleanup buffer below ``remaining_seconds`` for
    the runner's own teardown work (kill + final wait, stream-pump
    join). Without it the runner can overrun the state deadline by
    a few seconds doing post-timeout cleanup.
    """
    if remaining_seconds <= 0:
        raise TimeoutError(
            "State deadline expired before launching external process."
        )
    # Scale the buffer so it never eats more than 10% of remaining; for
    # 50+ seconds we get the full _PROCESS_CLEANUP_BUFFER_S, for tight
    # test fixtures (programming_timeout=5s) we reserve only ~0.5s.
    cleanup_buffer = min(_PROCESS_CLEANUP_BUFFER_S, remaining_seconds * 0.1)
    effective_remaining = remaining_seconds - cleanup_buffer
    if effective_remaining <= 0:
        raise TimeoutError(
            "State deadline expired before launching external process."
        )
    capped = min(request.timeout_seconds, effective_remaining)
    if capped >= request.timeout_seconds:
        return request
    return ExternalProcessRequest(
        file_name=request.file_name,
        arguments=request.arguments,
        working_directory=request.working_directory,
        timeout_seconds=capped,
        on_standard_output_line=request.on_standard_output_line,
        on_standard_error_line=request.on_standard_error_line,
        standard_input_response_factory=request.standard_input_response_factory,
    )


def _ensure_device_connected(device: Any) -> None:
    if not getattr(device, "is_connected", False):
        raise RuntimeError(
            "Device must be connected before starting firmware update."
        )


class _WrongBootloaderError(Exception):
    """The opened bootloader is not the one selected. Never retried."""


class _FlashVerificationError(Exception):
    """The flash's CRC differs from the image's. Never retried."""


_CLEANUP_GUIDANCE = {
    _RECOVERED:
        "The update did not complete, and the application flash was re-erased: "
        "the board stays in its PIC32 bootloader and no longer starts a partial "
        "application. This updater starts from the running application, so it "
        "can't re-flash the board from there; program it through its bootloader "
        "with another tool, or with a programmer (PICkit and MPLAB IPE).",
    _CLEANUP_FAILED:
        "The update failed partway and the flash could not be re-erased, or the "
        "re-erase couldn't be confirmed by reading the flash back, so the board "
        "may hold a half-written application that won't run. Reprogram it with "
        "a programmer (PICkit and MPLAB IPE).",
}


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



def _cmd_exe_arguments(
    script: str,
    template: str,
    port: str,
    firmware_path: str,
    *,
    native_windows: bool,
    version: Optional[str] = None,
) -> Union[str, List[str]]:
    """Arguments for ``cmd.exe`` running the WiFi flash script.

    Native Windows: one verbatim command line (SubprocessRunner passes a
    string through untouched there). ``/s`` strips exactly the outer quote
    pair, so the quoted pieces inside reach the script intact, and quoting
    every piece keeps cmd.exe metacharacters in configured values from being
    read as command syntax. A list would go through list2cmdline, which
    escapes inner quotes as ``\\"``; cmd.exe does not honour that, and the
    script was "not recognized".

    WSL: WSL rebuilds the Windows command line from argv the same way, so
    the arguments go separately and unquoted, and WSL quotes the ones with
    spaces itself.
    """
    # Neither host can pass these safely. cmd.exe expands %VAR% even inside
    # double quotes, so a path containing a literal %NAME% would reach the
    # tool as a different path; and a value containing a double quote can't
    # be quoted for cmd.exe at all. Under WSL, which leaves arguments without
    # whitespace unquoted, the other metacharacters would run as command
    # syntax ("&" in a path runs what follows it), and caret escapes would
    # arrive literally inside the arguments it does quote.
    unsafe_chars = _CMD_ALWAYS_UNSAFE if native_windows else _CMD_METACHARACTERS
    for name, value in (
        ("tool name", script), ("port", port), ("firmware path", firmware_path),
        ("firmware version", version or ""),
    ):
        unsafe = sorted(set(value) & unsafe_chars)
        if unsafe:
            raise ValueError(
                f"The WiFi flash {name} {value!r} contains {''.join(unsafe)!r}, "
                "which cmd.exe would interpret. Use a path or port name "
                "without cmd.exe special characters."
            )
    literal_text = _PLACEHOLDER.sub("", template)
    if native_windows and "%" in literal_text:
        # Quotes in the template survive into the line, so its own
        # metacharacters stay as written; %VAR% expands regardless.
        raise ValueError(
            f"The WiFi flash arguments template {template!r} contains '%', "
            "which cmd.exe would interpret."
        )
    if native_windows:
        # The template's own quotes are consumed by the split and each
        # argument is quoted once, whole, so a placeholder the template had
        # already quoted can't end up between two quote pairs, leaving its
        # value's metacharacters bare.
        written = _split_windows_template(template)
        filled = _substitute_placeholders(written, port, firmware_path, version)
        tool_arguments = " ".join(
            _quote_cmd_token(value, bool(_PLACEHOLDER.search(token)))
            for token, value in zip(written, filled)
        )
        command = f"{_quote_windows_argument(script)} {tool_arguments}".strip()
        # /v:off: with delayed expansion enabled in the registry, !VAR! would
        # expand inside the quotes as %VAR% does.
        return f'/d /v:off /s /c "{command}"'
    if any(ch.isspace() for ch in script):
        # WSL would quote it first, and cmd.exe /c strips a leading quote
        # when the line holds more than one quoted argument.
        raise ValueError(
            f"WiFi flash tool name {script!r} contains whitespace, which "
            "cmd.exe cannot run reliably when started from WSL; rename it."
        )
    tokens = _substitute_placeholders(
        _split_windows_template(template), port, firmware_path, version
    )
    for token in tokens:
        # The split removes a quoted literal's quotes, and WSL re-quotes only
        # tokens with whitespace, so "C:\fw&calc.exe&rem\x" from the
        # template itself would reach cmd.exe bare.
        unsafe = sorted(set(token) & _CMD_METACHARACTERS)
        if unsafe:
            raise ValueError(
                f"The WiFi flash arguments template {template!r} yields the "
                f"argument {token!r}, containing {''.join(unsafe)!r}, which "
                "cmd.exe would interpret when started from WSL."
            )
    return ["/d", "/v:off", "/c", script] + tokens


_VERSION_PLACEHOLDER = "{version}"
_PLACEHOLDER = re.compile(r"\{port\}|\{firmwarePath\}|\{version\}")


def _substitute_placeholders(
    tokens: List[str],
    port: str,
    firmware_path: str,
    version: Optional[str] = None,
) -> List[str]:
    """Fill the placeholders in already-split arguments, in one pass, so a
    value that itself contains ``{firmwarePath}`` is not substituted again.
    ``{version}`` is left as written when no version is given; the callers
    refuse that case before they get here."""
    values = {"{port}": port, "{firmwarePath}": firmware_path}
    if version is not None:
        values[_VERSION_PLACEHOLDER] = version
    return [
        _PLACEHOLDER.sub(lambda m: values.get(m.group(0), m.group(0)), t)
        for t in tokens
    ]


def _wifi_bundle_versions(device_directory: str) -> List[str]:
    """The firmware versions one device folder of a WINC bundle holds, sorted.

    A version counts only when its ``firmware/programmer_firmware.bin``
    exists, the file ``winc_flash_tool.cmd`` checks before it flashes.
    """
    try:
        candidates = os.listdir(device_directory)
    except OSError:
        return []
    return sorted(
        candidate for candidate in candidates
        if os.path.isfile(os.path.join(
            device_directory, candidate, "firmware", "programmer_firmware.bin"
        ))
    )


def _single_bundle_version(tool_path: str) -> str:
    """For a custom tool or template: the one version found across the
    bundle's ``files/winc*/`` folders beside the tool."""
    files_directory = os.path.join(os.path.dirname(tool_path), "files")
    try:
        names = os.listdir(files_directory)
    except OSError:
        names = []
    available = sorted({
        version
        for name in names if name.lower().startswith("winc")
        for version in _wifi_bundle_versions(os.path.join(files_directory, name))
    })
    if len(available) != 1:
        found = (
            f"it holds {', '.join(available)}" if available
            else "it holds no winc*/<version>/firmware/programmer_firmware.bin"
        )
        raise _WifiRequestRefused(
            "The WiFi flash arguments template passes {version}, and the "
            f"version could not be read from {files_directory!r}: {found}. "
            "Pass firmware_version=, the version download_wifi_firmware "
            "returns with the path. Nothing was sent."
        )
    return available[0]


def _is_default_wifi_setup(tool_path: str, template: str) -> bool:
    """Whether the update runs the shipped tool with the shipped arguments,
    the only setup whose arguments update_wifi_module checks in advance.

    By value, not by provenance: the options are a plain dataclass that
    records no history, and what is checked depends only on what runs, so a
    caller who sets the shipped values explicitly gets the same command and
    the same check. The tool is the file that will RUN, which is the path
    itself when the caller names a file, so its name is compared rather than
    the wifi_flash_tool_file_name option."""
    return (
        os.path.basename(tool_path) == DEFAULT_WIFI_FLASH_TOOL_FILE_NAME
        and template == DEFAULT_WIFI_FLASH_TOOL_ARGUMENTS_TEMPLATE
    )


# Allow-lists for the values substituted into the default arguments, rather
# than a model of cmd.exe and winc_flash_tool.cmd (see
# _build_default_wifi_request). Whatever is not listed is refused.
_WINC_VERSION_ALLOWED = re.compile(r"[0-9A-Za-z._-]+")
# COMn and /dev/tty* forms; /p always takes the next argument as its value.
_WINC_PORT_ALLOWED = re.compile(r"[A-Za-z0-9._:/-]+")


class _WifiRequestRefused(ValueError):
    """The WiFi update was refused at entry, before anything was sent.
    Raised to the caller as a ValueError."""


class _WifiPlan(NamedTuple):
    """What update_wifi_module settled at entry for the WiFi flow."""

    version: Optional[str] = None
    request: Optional[ExternalProcessRequest] = None
    error: Optional[BaseException] = None
    settings: Optional[Tuple[str, str]] = None


def _is_native_windows() -> bool:
    return os.name == "nt"


def _split_crt_template(template: str) -> List[str]:
    """Split an arguments template the way the C runtime splits a command line.

    The rules a Windows program applies to its own command line (MSVC CRT,
    as CommandLineToArgvW): whitespace separates arguments outside quotes;
    2n backslashes before a quote give n backslashes and the quote toggles
    quoting, 2n+1 give n backslashes and a literal quote; other backslashes
    are literal; "" inside quotes is a literal quote. Unlike shlex it never
    fails: an unclosed quote runs to the end.
    """
    arguments: List[str] = []
    current: List[str] = []
    in_argument = in_quotes = False
    i, n = 0, len(template)
    while i < n:
        ch = template[i]
        if ch == "\\":
            j = i
            while j < n and template[j] == "\\":
                j += 1
            run = j - i
            in_argument = True
            if j < n and template[j] == '"':
                current.append("\\" * (run // 2))
                if run % 2:
                    current.append('"')
                    j += 1
            else:
                current.append("\\" * run)
            i = j
        elif ch == '"':
            in_argument = True
            if in_quotes and i + 1 < n and template[i + 1] == '"':
                current.append('"')
                i += 2
            else:
                in_quotes = not in_quotes
                i += 1
        elif ch in " \t" and not in_quotes:
            if in_argument:
                arguments.append("".join(current))
                current, in_argument = [], False
            i += 1
        else:
            current.append(ch)
            in_argument = True
            i += 1
    if in_argument:
        arguments.append("".join(current))
    return arguments


def _split_posix_template(template: str) -> List[str]:
    """Split an arguments template the way a POSIX shell reads it.

    Raises ValueError for an unbalanced quote, while the request is still
    being built, before the device is switched into update mode.
    """
    try:
        return shlex.split(template)
    except ValueError as exc:
        raise ValueError(
            f"WiFi flash arguments template {template!r} can't be split: {exc}"
        ) from exc


def _quote_cmd_token(token: str, had_placeholder: bool) -> str:
    """One argument for the native cmd.exe line.

    Values are always quoted, so cmd.exe metacharacters in them stay
    literal; a template literal is quoted only when it needs it, so a
    switch such as ``/p`` reaches the script exactly as written.
    """
    if had_placeholder or not token or any(
        ch.isspace() or ch in "&|<>^()" for ch in token
    ):
        return _quote_windows_argument(token)
    return token


def _split_windows_template(template: str) -> List[str]:
    """Split a .cmd/.bat arguments template the way cmd.exe reads it.

    Double quotes group words (``"C:\\Firmware Files\\image.bin"`` is one
    argument, without its quotes), backslashes, apostrophes and ``#`` are
    ordinary characters, and outside quotes ``^`` escapes the next character
    (``A^&B`` is ``A&B``; inside quotes ``^`` is literal). Plain
    ``str.split`` kept the quotes and broke the argument in two; POSIX
    ``shlex`` ate the backslashes, and kept ``^`` so the script got ``^&``.
    An unclosed quote is refused while the request is built.
    """
    arguments: List[str] = []
    current: List[str] = []
    in_argument = in_quotes = False
    i, n = 0, len(template)
    while i < n:
        ch = template[i]
        if ch == '"':
            in_argument = True
            in_quotes = not in_quotes
        elif in_quotes:
            current.append(ch)
        elif ch == "^":
            in_argument = True
            if i + 1 < n:
                i += 1
                current.append(template[i])
        elif ch in " \t":
            if in_argument:
                arguments.append("".join(current))
                current, in_argument = [], False
        else:
            current.append(ch)
            in_argument = True
        i += 1
    if in_quotes:
        raise ValueError(
            f"WiFi flash arguments template {template!r} can't be split: "
            "No closing quotation"
        )
    if in_argument:
        arguments.append("".join(current))
    return arguments


_CMD_ALWAYS_UNSAFE = frozenset('%"')
_CMD_METACHARACTERS = frozenset('&|<>^%!"')



def _quote_windows_argument(value: str) -> str:
    """Quote for a Windows/cmd.exe command line, whatever the host.

    Always quoted, so cmd.exe metacharacters (``& | > < ^ ( )``) in a
    configured value can't be read as command syntax. Quoting does not stop
    ``%VAR%`` expansion, and a double quote can't be embedded reliably;
    callers refuse values containing either (see _cmd_exe_arguments).
    """
    if value is None or value == "":
        return '""'
    return '"' + value.replace('"', '""') + '"'


def _running_under_wsl() -> bool:
    try:
        with open("/proc/sys/kernel/osrelease") as handle:
            return "microsoft" in handle.read().lower()
    except OSError:
        return False


def _path_for_windows_tool(path: str) -> str:
    """A Linux path as a Windows program under WSL can open it (``wslpath -w``).

    Unchanged off WSL, for relative paths, or if the translation fails.
    """
    if not path.startswith("/") or not _running_under_wsl():
        return path
    try:
        result = subprocess.run(
            ["wslpath", "-w", path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return path
    translated = result.stdout.strip()
    return translated if result.returncode == 0 and translated else path


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


def _contains_marker(lines: Iterable[str], marker: str) -> bool:
    lowered = marker.lower()
    return any(lowered in line.lower() for line in lines)


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
