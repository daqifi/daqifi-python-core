#!/usr/bin/env python3
"""
Basic DAC (Analog Output) control example.

Demonstrates setting voltages on DAC channels (NQ3 only).
"""

import sys
from pathlib import Path

# Add parent directory to path for local testing
sys.path.insert(0, str(Path(__file__).parent.parent))

from daqifi import NyquistDevice, PowerState


def main():
    """Basic DAC control example"""

    # Connect to device (change port as needed)
    print("Connecting to device...")
    device = NyquistDevice.connect("/dev/ttyACM0")

    print(f"Connected to {device.variant.value}")
    print(f"Firmware: {device.firmware_version}")

    try:
        # Check if device has DAC (NQ3 only)
        if device.variant.value != "NQ3":
            print(f"Error: DAC not available on {device.variant.value}")
            print("DAC is only available on NQ3 variant")
            return

        # Power up (required for DAC - needs 10V rail)
        print("\nPowering up device...")
        device.power_up()

        # Set voltages on multiple channels
        print("\nSetting DAC voltages:")
        voltages = {
            0: 5.0,
            1: 3.3,
            2: 1.8,
        }

        for channel, voltage in voltages.items():
            print(f"  Channel {channel}: {voltage}V")
            device.analog_outputs[channel].voltage = voltage
            device.analog_outputs[channel].enabled = True

        # Read back voltages
        print("\nReading back voltages:")
        for channel in voltages.keys():
            actual = device.analog_outputs[channel].voltage
            enabled = device.analog_outputs[channel].enabled
            print(f"  Channel {channel}: {actual:.3f}V (enabled: {enabled})")

        # Alternative: Use bulk set
        print("\nUsing bulk voltage set:")
        device.analog_outputs.set_voltages({
            3: 7.5,
            4: 2.5,
            5: 0.5
        })

        # Read all channels
        print("\nAll DAC channels:")
        for ch in device.analog_outputs:
            print(f"  Channel {ch.index}: {ch.voltage:.3f}V (enabled: {ch.enabled})")

    finally:
        # Clean up
        print("\nDisconnecting...")
        device.disconnect()


if __name__ == "__main__":
    main()
