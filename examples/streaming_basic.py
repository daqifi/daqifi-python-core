#!/usr/bin/env python3
"""
Basic streaming example.

Demonstrates blocking streaming and data export.
"""

import sys
import time
from pathlib import Path

# Add parent directory to path for local testing
sys.path.insert(0, str(Path(__file__).parent.parent))

from daqifi import NyquistDevice


def main():
    """Basic streaming example"""

    # Connect to device
    print("Connecting to device...")
    device = NyquistDevice.connect("/dev/ttyACM0")

    print(f"Connected to {device.variant.value}")
    print(f"Firmware: {device.firmware_version}")

    try:
        # Configure ADC channels
        print("\nConfiguring ADC channels...")
        device.analog_inputs.configure_channels(
            channels=[0, 1, 2],
            range="10V",
            enabled=True
        )

        # Create stream
        print("\nCreating stream (channels 0-2, 1000 Hz)...")
        stream = device.create_stream(
            channels=[0, 1, 2],
            sample_rate=1000
        )

        print(f"Stream: {stream}")

        # Start streaming
        print("\nStarting stream...")
        stream.start()

        # Read samples for 5 seconds
        print("Streaming for 5 seconds...")
        buffer = stream.read_all(duration=5.0)

        print(f"Collected {len(buffer)} samples")

        # Stop streaming
        print("\nStopping stream...")
        stream.stop()

        # Display summary
        print("\nStream summary:")
        print(f"  Sample rate: {buffer.sample_rate} Hz")
        print(f"  Channels: {buffer.channels}")
        print(f"  Duration: {buffer.get_timestamps()[-1]:.2f} seconds")

        # Display first 5 samples
        print("\nFirst 5 samples:")
        for i, sample in enumerate(buffer[:5]):
            print(f"  {i}: {sample}")

        # Export data
        print("\nExporting data...")
        buffer.to_csv("streaming_data.csv")
        print("  Saved to streaming_data.csv")

        buffer.to_json("streaming_data.json")
        print("  Saved to streaming_data.json")

        # Export to DataFrame (if pandas available)
        try:
            df = buffer.to_dataframe()
            print(f"\nDataFrame shape: {df.shape}")
            print(f"Columns: {list(df.columns)}")
            print("\nFirst 5 rows:")
            print(df.head())
        except ImportError:
            print("\n  pandas not available (install with: pip install pandas)")

    finally:
        # Clean up
        print("\nDisconnecting...")
        device.disconnect()


if __name__ == "__main__":
    main()
