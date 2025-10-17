#!/usr/bin/env python3
"""
Context manager streaming example.

Demonstrates using context manager for automatic cleanup.
"""

import sys
from pathlib import Path

# Add parent directory to path for local testing
sys.path.insert(0, str(Path(__file__).parent.parent))

from daqifi import NyquistDevice


def main():
    """Context manager streaming example"""

    print("Connecting to device...")
    device = NyquistDevice.connect("/dev/ttyACM0")

    print(f"Connected to {device.variant.value}\n")

    try:
        # Configure ADC
        device.analog_inputs.configure_channels(
            channels=[0, 1, 2, 3],
            range="10V",
            enabled=True
        )

        # Use context manager - stream automatically starts/stops
        print("Creating stream with context manager...")
        with device.create_stream(channels=[0, 1, 2, 3], sample_rate=1000) as stream:
            print("Stream started automatically")

            # Read samples
            print("\nReading 1000 samples...")
            samples = stream.read(num_samples=1000)

            print(f"Read {len(samples)} samples")

            # Display some samples
            print("\nFirst 3 samples:")
            for i, sample in enumerate(samples[:3]):
                print(f"  {i}: {sample}")

            print("\nLast 3 samples:")
            for i, sample in enumerate(samples[-3:], len(samples)-3):
                print(f"  {i}: {sample}")

            # Get channel data
            ch0_data = [s[0] for s in samples]
            print(f"\nChannel 0 statistics:")
            print(f"  Min: {min(ch0_data):.3f}V")
            print(f"  Max: {max(ch0_data):.3f}V")
            print(f"  Avg: {sum(ch0_data)/len(ch0_data):.3f}V")

        # Stream automatically stopped when exiting context
        print("\nStream stopped automatically")

        # Access buffer after streaming
        buffer = stream.buffer
        print(f"\nBuffer contains {len(buffer)} samples")

        # Export
        print("\nExporting to CSV...")
        buffer.to_csv("context_streaming.csv")
        print("  Saved to context_streaming.csv")

    finally:
        print("\nDisconnecting...")
        device.disconnect()


if __name__ == "__main__":
    main()
