#!/usr/bin/env python3
"""
Callback-based streaming example.

Demonstrates real-time streaming with callback function.
"""

import sys
import time
from pathlib import Path

# Add parent directory to path for local testing
sys.path.insert(0, str(Path(__file__).parent.parent))

from daqifi import NyquistDevice, Sample


def main():
    """Callback-based streaming example"""

    # Sample counter
    sample_count = [0]  # Use list for mutable closure
    start_time = [None]

    def on_sample(sample: Sample):
        """Callback function called for each sample"""
        if start_time[0] is None:
            start_time[0] = sample.timestamp

        sample_count[0] += 1

        # Print every 100th sample
        if sample_count[0] % 100 == 0:
            elapsed = sample.timestamp - start_time[0]
            rate = sample_count[0] / elapsed if elapsed > 0 else 0
            print(f"Sample {sample_count[0]:5d}: t={elapsed:.2f}s, rate={rate:.1f} Hz, {sample}")

    # Connect to device
    print("Connecting to device...")
    device = NyquistDevice.connect("/dev/ttyACM0")

    print(f"Connected to {device.variant.value}")

    try:
        # Configure ADC channels
        print("\nConfiguring ADC channels 0-1...")
        device.analog_inputs.configure_channels(
            channels=[0, 1],
            range="10V",
            enabled=True
        )

        # Create stream
        stream = device.create_stream(
            channels=[0, 1],
            sample_rate=1000
        )

        # Start streaming with callback
        print("\nStarting stream with callback...")
        print("Will stream for 10 seconds...\n")

        stream.start(callback=on_sample)

        # Let it run for 10 seconds
        time.sleep(10.0)

        # Stop streaming
        print("\nStopping stream...")
        stream.stop()

        # Display summary
        total_samples = sample_count[0]
        duration = time.time() - start_time[0] if start_time[0] else 0
        avg_rate = total_samples / duration if duration > 0 else 0

        print(f"\nStreaming summary:")
        print(f"  Total samples: {total_samples}")
        print(f"  Duration: {duration:.2f} seconds")
        print(f"  Average rate: {avg_rate:.1f} Hz")

        # Access buffer (samples collected during streaming)
        buffer = stream.buffer
        print(f"  Buffered samples: {len(buffer)}")

        # Export buffered data
        if len(buffer) > 0:
            print("\nExporting buffered data...")
            buffer.to_csv("callback_streaming.csv")
            print("  Saved to callback_streaming.csv")

    finally:
        # Clean up
        print("\nDisconnecting...")
        device.disconnect()


if __name__ == "__main__":
    main()
