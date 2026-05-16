#!/usr/bin/env python3
import os
import sys

from daqifi import NyquistDevice

# Per CLAUDE.md: resolve serial port from CLI arg or env var, never
# from a hardcoded workstation-specific identifier.
port = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DAQIFI_DEVICE_PORT')
if not port:
    sys.exit("usage: test_benchmark_debug.py <port>  (or set DAQIFI_DEVICE_PORT)")

# Connect
print(f"Connecting to {port}...")
device = NyquistDevice.connect(port)
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
