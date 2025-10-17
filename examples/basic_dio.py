#!/usr/bin/env python3
"""
Basic Digital I/O control example.

Demonstrates configuring and controlling digital I/O channels.
"""

import sys
import time
from pathlib import Path

# Add parent directory to path for local testing
sys.path.insert(0, str(Path(__file__).parent.parent))

from daqifi import NyquistDevice


def main():
    """Basic DIO control example"""

    # Connect to device (change port as needed)
    print("Connecting to device...")
    device = NyquistDevice.connect("/dev/ttyACM0")

    print(f"Connected to {device.variant.value}")
    print(f"Firmware: {device.firmware_version}")

    try:
        # Configure channels 0-2 as outputs
        print("\nConfiguring channels 0-2 as outputs:")
        device.digital_io.configure_outputs([0, 1, 2])

        # Set output states
        print("Setting output states:")
        device.digital_io[0].state = True   # High
        device.digital_io[1].state = False  # Low
        device.digital_io[2].state = True   # High

        # Read back states
        for ch_id in [0, 1, 2]:
            ch = device.digital_io[ch_id]
            print(f"  Channel {ch_id}: {ch.direction}, state={ch.state}")

        # Configure channels 3-5 as inputs
        print("\nConfiguring channels 3-5 as inputs:")
        device.digital_io.configure_inputs([3, 4, 5])

        # Read input states
        print("Reading input states:")
        for ch_id in [3, 4, 5]:
            ch = device.digital_io[ch_id]
            print(f"  Channel {ch_id}: {ch.direction}, state={ch.state}")

        # Blink output channel 0
        print("\nBlinking channel 0 (5 times):")
        for i in range(5):
            device.digital_io[0].state = True
            print("  High")
            time.sleep(0.5)

            device.digital_io[0].state = False
            print("  Low")
            time.sleep(0.5)

        # Bulk state set
        print("\nBulk state set:")
        device.digital_io.set_states({
            0: True,
            1: True,
            2: False
        })

        # Read all DIO states
        print("\nAll DIO channels:")
        all_states = device.digital_io.read_all()
        for ch_id, state in all_states.items():
            ch = device.digital_io[ch_id]
            print(f"  Channel {ch_id}: {ch.direction}, state={state}")

    finally:
        # Clean up
        print("\nDisconnecting...")
        device.disconnect()


if __name__ == "__main__":
    main()
