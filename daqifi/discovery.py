"""
Device discovery for DAQiFi Nyquist devices.

Provides automatic detection of devices connected via USB/Serial.
"""

import glob
import platform
from typing import List, Optional
from dataclasses import dataclass
from enum import Enum


class BoardVariant(Enum):
    """Nyquist board variants"""
    UNKNOWN = "UNKNOWN"
    NQ1 = "NQ1"
    NQ2 = "NQ2"
    NQ3 = "NQ3"


@dataclass
class DeviceInfo:
    """Information about a discovered device"""
    port: str
    variant: BoardVariant = BoardVariant.UNKNOWN
    manufacturer: str = ""
    model: str = ""
    serial_number: str = ""
    firmware_version: str = ""

    def __repr__(self) -> str:
        if self.model:
            return f"DeviceInfo(port={self.port}, variant={self.variant.value}, model={self.model})"
        return f"DeviceInfo(port={self.port})"



# *IDN? probe timing (``_identify_device``) and its longest healthy run, from
# SerialInterface's own waits: open sleeps 0.1 s, writes the echo-off with a
# write timeout of the port timeout, waits 0.1 s and drains up to 0.5 s; the
# query waits its delay, writes, and reads until the port timeout with one
# read that can start just before it; close may write once more.
_IDENTIFY_PORT_TIMEOUT_S = 0.5
_IDENTIFY_QUERY_DELAY_S = 0.2
_IDENTIFY_WORST_HEALTHY_S = (
    0.1 + _IDENTIFY_PORT_TIMEOUT_S + 0.1 + 0.5         # open
    + _IDENTIFY_QUERY_DELAY_S + _IDENTIFY_PORT_TIMEOUT_S
    + 2 * _IDENTIFY_PORT_TIMEOUT_S                      # query
    + _IDENTIFY_PORT_TIMEOUT_S                          # close
)
# Plus room for the port open itself; a probe past it is abandoned.
_IDENTIFY_HARD_TIMEOUT_S = _IDENTIFY_WORST_HEALTHY_S + 3.0
assert _IDENTIFY_HARD_TIMEOUT_S > _IDENTIFY_WORST_HEALTHY_S


class DeviceFinder:
    """
    Device discovery for Nyquist devices.

    Searches for devices connected via USB/Serial ports.
    """

    @staticmethod
    def get_serial_ports() -> List[str]:
        """
        Get the serial ports that may be a DAQiFi board.

        Only USB serial ports with the DAQiFi application's VID/PID
        (04D8:F794) are listed, plus CDC-ACM ports whose USB IDs can't be
        read, so discovery never writes to an unrelated device. Each board
        is listed once (on macOS as its ``/dev/cu.*`` node). Empty when the
        ports can't be listed.

        Returns:
            List of serial port paths

        Example:
            ports = DeviceFinder.get_serial_ports()
            # Linux: ['/dev/ttyACM0']; Windows: the board's COM port
        """
        from ._internal.usb_ports import daqifi_serial_ports

        return daqifi_serial_ports()

    @staticmethod
    def _identify_device(port: str) -> Optional[DeviceInfo]:
        """
        Try to identify device on given port.

        Args:
            port: Serial port path

        Returns:
            DeviceInfo if device responds, None otherwise
        """
        try:
            from ._internal.comm import SerialInterface

            # Try to open and query device
            comm = SerialInterface(port, timeout=_IDENTIFY_PORT_TIMEOUT_S)

            if not comm.open():
                return None

            try:
                # Query device identification
                idn = comm.send_query("*IDN?", delay=_IDENTIFY_QUERY_DELAY_S)

                if not idn or len(idn) == 0:
                    return None

                # Parse IDN response: "Manufacturer,Model,Serial,Firmware"
                parts = idn.split(",")

                info = DeviceInfo(port=port)

                if len(parts) >= 1:
                    info.manufacturer = parts[0].strip()
                if len(parts) >= 2:
                    info.model = parts[1].strip()
                    # Detect variant from model
                    model_upper = info.model.upper()
                    if "NQ3" in model_upper:
                        info.variant = BoardVariant.NQ3
                    elif "NQ2" in model_upper:
                        info.variant = BoardVariant.NQ2
                    elif "NQ1" in model_upper:
                        info.variant = BoardVariant.NQ1
                if len(parts) >= 3:
                    info.serial_number = parts[2].strip()
                if len(parts) >= 4:
                    info.firmware_version = parts[3].strip()

                return info

            finally:
                comm.close()

        except Exception:
            # Failed to communicate with device
            return None

    @staticmethod
    def discover(variant: Optional[BoardVariant] = None) -> List[DeviceInfo]:
        """
        Discover all connected Nyquist devices.

        Args:
            variant: Optional board variant filter (NQ1, NQ2, NQ3)

        Returns:
            List of discovered devices

        Example:
            finder = DeviceFinder()

            # Find all devices
            devices = finder.discover()

            # Find only NQ3 devices
            nq3_devices = finder.discover(variant=BoardVariant.NQ3)
        """
        discovered = []

        # Get all serial ports
        ports = DeviceFinder.get_serial_ports()

        # Try to identify device on each port. Each probe is bounded: a port
        # that hangs on open or read is abandoned, not waited on forever.
        from ._internal.usb_ports import run_port_probe

        for port in ports:
            device_info = run_port_probe(
                port, DeviceFinder._identify_device, _IDENTIFY_HARD_TIMEOUT_S
            )

            if device_info:
                # Apply variant filter if specified
                if variant is None or device_info.variant == variant:
                    discovered.append(device_info)

        return discovered

    @staticmethod
    def discover_first(variant: Optional[BoardVariant] = None) -> Optional[DeviceInfo]:
        """
        Discover first available device.

        Args:
            variant: Optional board variant filter

        Returns:
            First discovered device, or None if no device found

        Example:
            device_info = DeviceFinder.discover_first()
            if device_info:
                device = NyquistDevice.connect(device_info)
        """
        devices = DeviceFinder.discover(variant=variant)
        return devices[0] if devices else None


@dataclass(frozen=True)
class DeviceDiscoveredEventArgs:
    """Payload for device-discovered event callbacks.

    Ports the C# ``DeviceDiscoveredEventArgs`` (subclass of ``EventArgs``)
    from ``Daqifi.Core/Device/Discovery/DeviceDiscoveredEventArgs.cs``.
    Per the project port convention, ``EventArgs`` becomes a frozen
    dataclass — the upstream type exposes only ``get;`` properties so
    the payload is immutable once constructed.

    The C# constructor null-checks ``deviceInfo``; here we rely on
    duck-typing and let the caller pass any object exposing the
    ``DeviceInfo`` shape (matching :class:`DeviceInfo` above).
    """

    device_info: DeviceInfo
