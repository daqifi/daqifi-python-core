#!/usr/bin/env python3
"""
Basic ADC (Analog Input) configuration example.

Demonstrates configuring ADC channels and reading configuration.
"""

import sys
from pathlib import Path

# Add parent directory to path for local testing
sys.path.insert(0, str(Path(__file__).parent.parent))

from daqifi import BoardVariant, NyquistDevice


def main():
    """Basic ADC configuration example"""

    # Connect to device (change port as needed)
    print("Connecting to device...")
    device = NyquistDevice.connect("/dev/ttyACM0")

    print(f"Connected to {device.variant.value}")
    print(f"Firmware: {device.firmware_version}")

    try:
        # Configure individual channel
        print("\nConfiguring ADC channel 0:")
        if device.variant == BoardVariant.NQ3:
            # NQ1/NQ2 have a fixed input range; setting it raises there.
            device.analog_inputs[0].range = "10V"
        device.analog_inputs[0].enabled = True
        print(f"  Range: {device.analog_inputs[0].range}")
        print(f"  Enabled: {device.analog_inputs[0].enabled}")

        # Configure multiple channels at once
        print("\nConfiguring channels 1-3:")
        device.analog_inputs.configure_channels(
            channels=[1, 2, 3],
            range="5V",
            enabled=True
        )

        # Read back configuration
        print("\nAll ADC channel configurations:")
        for ch in device.analog_inputs:
            print(f"  Channel {ch.index}: range={ch.range}, enabled={ch.enabled}")

        # Disable all channels
        print("\nDisabling all channels...")
        device.analog_inputs.disable_all()

        # Verify all disabled
        enabled_count = sum(1 for ch in device.analog_inputs if ch.enabled)
        print(f"Enabled channels: {enabled_count}")

    finally:
        # Clean up
        print("\nDisconnecting...")
        device.disconnect()


if __name__ == "__main__":
    main()
