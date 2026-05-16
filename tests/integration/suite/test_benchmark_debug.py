#!/usr/bin/env python3
import sys

from daqifi import NyquistDevice

# Connect
print("Connecting to COM3...")
device = NyquistDevice.connect("COM3")
print(f"Connected to {device.variant.value}\n")

# Enable SD
print("Enabling SD card...")
device._comm.send_command("SYST:STOR:SD:ENAble 1")

# Run benchmark
print("Running benchmark (5 seconds)...")
response = device._comm.send_command("SYST:STOR:SD:BENCHmark 1024")
print(f"\nBenchmark response:\n{response}\n")

# Query results - show RAW output
print("Querying results...")
results = device._comm.send_command("SYST:STOR:SD:BENCHmark?")

print("="*80)
print("RAW RESPONSE:")
print("="*80)
print(repr(results))
print("="*80)
print("\nFormatted:")
print(results)
print("="*80)

# Try to parse
print("\nParsing lines...")
for i, line in enumerate(results.split('\n')):
    stripped = line.strip()
    print(f"Line {i}: {repr(stripped)}")
    if ',' in stripped:
        parts = stripped.split(',')
        print(f"  -> {len(parts)} parts: {parts}")

device.disconnect()
