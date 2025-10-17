#!/usr/bin/env python3
"""
Async streaming example.

Demonstrates async/await streaming interface.
Requires Python 3.7+.
"""

import sys
import asyncio
from pathlib import Path

# Add parent directory to path for local testing
sys.path.insert(0, str(Path(__file__).parent.parent))

from daqifi import NyquistDevice


async def stream_async():
    """Async streaming example"""

    # Connect to device
    print("Connecting to device...")
    device = NyquistDevice.connect("/dev/ttyACM0")

    print(f"Connected to {device.variant.value}\n")

    try:
        # Configure ADC
        device.analog_inputs.configure_channels(
            channels=[0, 1],
            range="10V",
            enabled=True
        )

        # Create async stream
        print("Creating async stream...")
        async with device.create_stream([0, 1], sample_rate=1000, async_mode=True) as stream:
            print("Stream started")

            # Read samples asynchronously
            print("\nReading 500 samples asynchronously...")
            samples = await stream.read(num_samples=500)

            print(f"Read {len(samples)} samples")

            # Display first/last samples
            print("\nFirst 3 samples:")
            for i, sample in enumerate(samples[:3]):
                print(f"  {i}: {sample}")

            print("\nLast 3 samples:")
            for i, sample in enumerate(samples[-3:], len(samples)-3):
                print(f"  {i}: {sample}")

        print("\nStream stopped")

    finally:
        print("\nDisconnecting...")
        device.disconnect()


async def stream_iterator():
    """Async iterator streaming example"""

    print("\n" + "="*60)
    print("Async Iterator Example")
    print("="*60 + "\n")

    # Connect to device
    print("Connecting to device...")
    device = NyquistDevice.connect("/dev/ttyACM0")

    try:
        # Configure ADC
        device.analog_inputs.configure_channels(
            channels=[0],
            range="10V",
            enabled=True
        )

        # Create async stream and iterate
        print("Streaming with async iterator...")
        stream = device.create_stream([0], sample_rate=100, async_mode=True)
        await stream.start()

        sample_count = 0
        async for sample in stream:
            print(f"Sample {sample_count}: {sample}")
            sample_count += 1

            # Stop after 10 samples
            if sample_count >= 10:
                break

        await stream.stop()
        print(f"\nReceived {sample_count} samples via iterator")

    finally:
        print("\nDisconnecting...")
        device.disconnect()


def main():
    """Main entry point"""

    print("="*60)
    print("Async Streaming Examples")
    print("="*60 + "\n")

    # Run async examples
    print("1. Async read:")
    asyncio.run(stream_async())

    print("\n" + "="*60 + "\n")

    print("2. Async iterator:")
    asyncio.run(stream_iterator())


if __name__ == "__main__":
    main()
