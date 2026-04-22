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


class DeviceFinder:
    """
    Device discovery for Nyquist devices.

    Searches for devices connected via USB/Serial ports.
    """

    @staticmethod
    def get_serial_ports() -> List[str]:
        """
        Get list of available serial ports.

        Returns:
            List of serial port paths

        Example:
            ports = DeviceFinder.get_serial_ports()
            # Linux: ['/dev/ttyACM0', '/dev/ttyACM1']
            # Windows: ['COM3', 'COM4']
        """
        system = platform.system()

        if system == "Linux":
            # Look for USB CDC ACM devices (typical for Nyquist)
            ports = glob.glob('/dev/ttyACM*')
            # Also check for USB serial devices
            ports.extend(glob.glob('/dev/ttyUSB*'))
            return sorted(ports)

        elif system == "Windows":
            # Windows COM ports
            ports = []
            for i in range(1, 256):
                port = f'COM{i}'
                try:
                    # Try to check if port exists (quick check)
                    # Actual connection test will happen in discover()
                    ports.append(port)
                except:
                    pass
            return ports

        elif system == "Darwin":  # macOS
            # macOS USB devices
            ports = glob.glob('/dev/tty.usbmodem*')
            ports.extend(glob.glob('/dev/tty.usbserial*'))
            ports.extend(glob.glob('/dev/cu.usbmodem*'))
            ports.extend(glob.glob('/dev/cu.usbserial*'))
            return sorted(ports)

        else:
            # Unknown system, return empty list
            return []

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
            comm = SerialInterface(port, timeout=0.5)

            if not comm.open():
                return None

            try:
                # Query device identification
                idn = comm.send_query("*IDN?", delay=0.2)

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

        # Try to identify device on each port
        for port in ports:
            device_info = DeviceFinder._identify_device(port)

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
