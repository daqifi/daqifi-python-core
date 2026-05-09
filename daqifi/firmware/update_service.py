"""Protocol declaration for the firmware update orchestrator.

Ports the C# ``IFirmwareUpdateService`` interface from
``Daqifi.Core.Firmware.IFirmwareUpdateService``. Sync per project
convention — implementations block until the device responds.

The concrete implementation (``FirmwareUpdateService.cs``, 1156 lines
upstream) is deferred to a follow-up PR; this module only provides the
contract so callers can type against the interface today.

Divergences from upstream:
- Methods drop the ``Async`` suffix and return ``None`` instead of
  ``Task`` (sync end-to-end, mirrors ``NetworkConfigurable``,
  ``SdCardOperations``, etc.).
- The C# ``event StateChanged`` becomes a ``state_changed`` callable
  attribute — implementations expose it as a ``Optional[Callable[
  [FirmwareUpdateStateChangedEventArgs], None]]`` slot the caller can
  set / clear. One handler at a time; multiplex with a fan-out wrapper
  if multiple observers need it.
- ``progress`` is typed ``Optional[Callable[[FirmwareUpdateProgress],
  None]]`` rather than ``IProgress<T>`` (Pythonic; mirrors
  ``SdCardParseOptions.progress``).
- ``cancellationToken`` is omitted — sync, single-shot operations don't
  need cooperative cancellation. Wrap in a thread + signal ``Event`` if
  you need it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional, Protocol, runtime_checkable

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.update_events import (
    FirmwareUpdateProgress,
    FirmwareUpdateStateChangedEventArgs,
)


if TYPE_CHECKING:
    # Forward reference only — avoid circular import with daqifi.devices.
    from daqifi.devices import DaqifiStreamingDevice


__all__ = ["FirmwareUpdateService"]


@runtime_checkable
class FirmwareUpdateService(Protocol):
    """Protocol for orchestrating firmware updates on a DAQiFi device.

    Pythonic name for the C# ``IFirmwareUpdateService`` interface
    (the ``I`` prefix is dropped to match the rest of this codebase's
    Protocols).

    All methods are synchronous — see module docstring. Concrete
    implementations block until the update finishes (or raises).
    """

    @property
    def current_state(self) -> FirmwareUpdateState:
        """The orchestrator's current update state."""
        ...

    state_changed: Optional[
        Callable[[FirmwareUpdateStateChangedEventArgs], None]
    ]
    """Optional observer callback invoked on every state transition.

    Set to a callable to observe; set to ``None`` to detach.
    Implementations should not synchronously invoke update operations
    on the same service instance from inside this callback (matches
    the upstream ``StateChanged`` event contract).
    """

    def update_firmware(
        self,
        device: "DaqifiStreamingDevice",
        hex_file_path: str,
        progress: Optional[Callable[[FirmwareUpdateProgress], None]] = None,
    ) -> None:
        """Run a PIC32 firmware update via bootloader mode.

        Args:
            device: The connected streaming device to update.
            hex_file_path: Path to an Intel HEX firmware file.
            progress: Optional progress callback invoked periodically
                with a :class:`FirmwareUpdateProgress` snapshot.

        Raises:
            FirmwareUpdateException: when any update step fails.
        """
        ...

    def update_wifi_module(
        self,
        device: "DaqifiStreamingDevice",
        firmware_path: str,
        progress: Optional[Callable[[FirmwareUpdateProgress], None]] = None,
    ) -> None:
        """Run a WiFi-module update via the external flashing tool.

        Args:
            device: The connected streaming device to update.
            firmware_path: Path to a WiFi tool executable / script,
                or a directory containing it.
            progress: Optional progress callback invoked periodically
                with a :class:`FirmwareUpdateProgress` snapshot.

        Raises:
            FirmwareUpdateException: when any update step fails.
        """
        ...
