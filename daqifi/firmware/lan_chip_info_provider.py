"""Optional capability for querying the WiFi LAN chip info from a device.

Ports the C# ``ILanChipInfoProvider`` interface from
``Daqifi.Core.Firmware.ILanChipInfoProvider``. Sync per project
convention — implementations block until the device responds (matches
the rest of this codebase's device-side Protocols like
``NetworkConfigurable``, ``SdCardOperations``, etc.).

Implement on a device class to enable WiFi firmware version checks
before flashing.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from daqifi.firmware.lan_chip_info import LanChipInfo


__all__ = ["LanChipInfoProvider"]


@runtime_checkable
class LanChipInfoProvider(Protocol):
    """Protocol for devices that can query their WiFi module's chip info.

    Pythonic name for the C# ``ILanChipInfoProvider`` interface (the
    ``I`` prefix is dropped to match the rest of this codebase's
    Protocols). Synchronous — see module docstring.
    """

    def get_lan_chip_info(self) -> Optional[LanChipInfo]:
        """Query the device for its current WiFi module chip info.

        Returns:
            The parsed :class:`LanChipInfo`, or ``None`` if the device
            did not return a recognizable response.

        Raises:
            RuntimeError: if the device is not connected.
        """
        ...
