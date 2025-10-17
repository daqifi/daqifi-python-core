#!/usr/bin/env python3
"""
Device discovery example.

Demonstrates automatic discovery of connected Nyquist devices.
"""

import sys
from pathlib import Path

# Add parent directory to path for local testing
sys.path.insert(0, str(Path(__file__).parent.parent))

from daqifi import DeviceFinder, NyquistDevice, BoardVariant


def main():
    """Device discovery example"""

    print("DAQiFi Device Discovery\n")

    # List all serial ports
    print("Available serial ports:")
    ports = DeviceFinder.get_serial_ports()
    for port in ports:
        print(f"  {port}")

    if not ports:
        print("  No serial ports found")
        return

    print("\nSearching for DAQiFi devices...")

    # Discover all devices
    devices = DeviceFinder.discover()

    if not devices:
        print("No DAQiFi devices found")
        return

    print(f"\nFound {len(devices)} device(s):\n")

    # Print device info
    for i, device_info in enumerate(devices, 1):
        print(f"Device {i}:")
        print(f"  Port:     {device_info.port}")
        print(f"  Variant:  {device_info.variant.value}")
        print(f"  Model:    {device_info.model}")
        print(f"  Serial:   {device_info.serial_number}")
        print(f"  Firmware: {device_info.firmware_version}")
        print()

    # Find specific variant
    print("Searching for NQ3 devices only...")
    nq3_devices = DeviceFinder.discover(variant=BoardVariant.NQ3)
    print(f"Found {len(nq3_devices)} NQ3 device(s)")

    # Connect to first device
    if devices:
        print(f"\nConnecting to first device ({devices[0].port})...")
        device = NyquistDevice.connect(devices[0])

        print(f"Connected successfully!")
        print(f"Device: {device}")
        print(f"Power state: {device.power_state.name}")

        # Get system info
        try:
            sys_info = device.get_system_info()
            print(f"\nSystem info:\n{sys_info}")
        except:
            print("\nSystem info not available")

        device.disconnect()
        print("\nDisconnected")


if __name__ == "__main__":
    main()
