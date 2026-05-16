#!/usr/bin/env python3
"""
Test to isolate USB transfer corruption: Firmware vs Python

This script will:
1. Download a file using Python script
2. Capture raw USB data using different methods
3. Compare to identify where corruption occurs
"""

import sys
import os
import hashlib
from pathlib import Path

from daqifi import NyquistDevice


def calculate_checksum(data):
    """Calculate MD5 checksum of data"""
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.md5(data).hexdigest()


def test_python_download(port, filename, output_dir='./test_output'):
    """Download file using Python script"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    print("="*80)
    print("TEST 1: Python Script Download")
    print("="*80)

    try:
        # Connect
        print(f"\nConnecting to {port}...")
        device = NyquistDevice.connect(port)
        print(f"✓ Connected")

        # Enable SD
        device.sd_enable(True)
        print("✓ SD enabled")

        # Download file
        print(f"\nDownloading '{filename}'...")
        content = device.sd_get_file(filename, timeout=300.0)

        # Save with Python script's method (TEXT mode - may corrupt)
        output_file_text = output_path / f"{filename}.python_text"
        with open(output_file_text, 'w', encoding='utf-8') as f:
            f.write(content)

        # Save with BINARY mode (correct)
        output_file_binary = output_path / f"{filename}.python_binary"
        with open(output_file_binary, 'wb') as f:
            f.write(content.encode('utf-8'))

        print(f"✓ Downloaded {len(content):,} bytes")
        print(f"  Text mode:   {output_file_text}")
        print(f"  Binary mode: {output_file_binary}")

        # Calculate checksums
        checksum = calculate_checksum(content)
        print(f"\n  Checksum (in-memory): {checksum}")

        # Check line count
        line_count = content.count('\n')
        print(f"  Line count: {line_count:,}")

        # Check for CRLF vs LF
        crlf_count = content.count('\r\n')
        lf_only_count = content.count('\n') - crlf_count
        print(f"  Line endings: {crlf_count:,} CRLF, {lf_only_count:,} LF")

        device.disconnect()
        return {
            'content': content,
            'checksum': checksum,
            'line_count': line_count,
            'crlf_count': crlf_count,
            'lf_count': lf_only_count,
            'size': len(content)
        }

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_raw_serial_capture(port, filename, output_dir='./test_output'):
    """Capture raw USB data using pyserial directly"""
    import serial
    import time

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    print("\n" + "="*80)
    print("TEST 2: Raw Serial Capture")
    print("="*80)

    try:
        # Open serial port
        print(f"\nOpening {port}...")
        ser = serial.Serial(port, 115200, timeout=1)
        time.sleep(0.5)

        # Clear any pending data
        ser.reset_input_buffer()

        # Send command
        cmd = f'SYST:STOR:SD:GET "{filename}"\r'
        print(f"Sending command: {cmd.strip()}")
        ser.write(cmd.encode('ascii'))
        ser.flush()

        # Read response.
        # NB: bytearray + extend (not `response += chunk` on bytes) —
        # bytes concatenation is O(n) per chunk, O(n²) over the whole
        # transfer, which is painful at multi-MB SD downloads.
        # Marker check uses a rolling tail buffer so the scan stays
        # constant-time per chunk instead of growing O(n²) across the
        # download.
        print("Reading response...")
        response = bytearray()
        end_marker = b"__END_OF_FILE__"
        tail = bytearray()
        tail_keep = len(end_marker) + 64
        # Absolute deadline + inactivity timeout: an inactivity-only
        # timer would never exit on a trickling stream.
        timeout = 300.0
        deadline = time.monotonic() + timeout
        last_rx = time.monotonic()
        last_progress_mb = 0

        while time.monotonic() < deadline and (time.monotonic() - last_rx) < timeout:
            if ser.in_waiting > 0:
                chunk = ser.read(ser.in_waiting)
                response.extend(chunk)
                tail.extend(chunk)
                if len(tail) > tail_keep:
                    del tail[:-tail_keep]

                if end_marker in tail:
                    marker_idx = response.find(end_marker)
                    if marker_idx != -1:
                        # Truncate AT the marker (exclude it). The
                        # marker is a protocol terminator, not part of
                        # the payload — keeping it corrupts the saved
                        # binary and mismatches sd_get_file()'s output.
                        del response[marker_idx:]
                    break

                mb = len(response) // (1024 * 1024)
                if mb > last_progress_mb:
                    print(f"  Received: {len(response):,} bytes...")
                    last_progress_mb = mb

                last_rx = time.monotonic()  # Reset inactivity timer
            else:
                time.sleep(0.01)

        ser.close()

        # Save raw response
        output_file_raw = output_path / f"{filename}.raw_binary"
        with open(output_file_raw, 'wb') as f:
            f.write(response)

        print(f"\n✓ Captured {len(response):,} bytes")
        print(f"  Raw output: {output_file_raw}")

        # Analyze raw response
        checksum_raw = hashlib.md5(response).hexdigest()
        print(f"\n  Checksum (raw): {checksum_raw}")

        # Check line endings in raw data
        crlf_count = response.count(b'\r\n')
        lf_only_count = response.count(b'\n') - crlf_count
        print(f"  Line endings: {crlf_count:,} CRLF, {lf_only_count:,} LF")

        # Decode for analysis
        response_str = response.decode('ascii', errors='ignore')
        line_count = response_str.count('\n')
        print(f"  Line count: {line_count:,}")

        return {
            'response_raw': response,
            'response_str': response_str,
            'checksum': checksum_raw,
            'line_count': line_count,
            'crlf_count': crlf_count,
            'lf_count': lf_only_count,
            'size': len(response)
        }

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return None


def compare_results(result1, result2, reference_file=None):
    """Compare results from different methods"""
    print("\n" + "="*80)
    print("COMPARISON")
    print("="*80)

    if not result1 or not result2:
        print("✗ Missing test results")
        return

    print(f"\n{'Metric':<20} {'Python Script':<20} {'Raw Serial':<20} {'Match':<10}")
    print("-"*80)

    # Compare sizes
    match_size = result1['size'] == result2['size']
    print(f"{'Size (bytes)':<20} {result1['size']:<20,} {result2['size']:<20,} {'✓' if match_size else '✗':<10}")

    # Compare checksums
    match_checksum = result1['checksum'] == result2['checksum']
    print(f"{'Checksum':<20} {result1['checksum']:<20} {result2['checksum']:<20} {'✓' if match_checksum else '✗':<10}")

    # Compare line counts
    match_lines = result1['line_count'] == result2['line_count']
    print(f"{'Line count':<20} {result1['line_count']:<20,} {result2['line_count']:<20,} {'✓' if match_lines else '✗':<10}")

    # Compare line endings
    print(f"{'CRLF endings':<20} {result1['crlf_count']:<20,} {result2['crlf_count']:<20,}")
    print(f"{'LF endings':<20} {result1['lf_count']:<20,} {result2['lf_count']:<20,}")

    # Compare to reference file (SD direct read)
    if reference_file and os.path.exists(reference_file):
        print(f"\n{'Metric':<20} {'USB Transfer':<20} {'SD Direct Read':<20} {'Match':<10}")
        print("-"*80)

        with open(reference_file, 'rb') as f:
            ref_data = f.read()

        ref_checksum = hashlib.md5(ref_data).hexdigest()
        ref_size = len(ref_data)
        ref_lines = ref_data.count(b'\n')
        ref_crlf = ref_data.count(b'\r\n')
        ref_lf = ref_lines - ref_crlf

        match_ref_size = result2['size'] == ref_size
        match_ref_checksum = result2['checksum'] == ref_checksum

        print(f"{'Size (bytes)':<20} {result2['size']:<20,} {ref_size:<20,} {'✓' if match_ref_size else '✗':<10}")
        print(f"{'Checksum':<20} {result2['checksum']:<20} {ref_checksum:<20} {'✓' if match_ref_checksum else '✗':<10}")
        print(f"{'Line count':<20} {result2['line_count']:<20,} {ref_lines:<20,}")
        print(f"{'CRLF endings':<20} {result2['crlf_count']:<20,} {ref_crlf:<20,}")
        print(f"{'LF endings':<20} {result2['lf_count']:<20,} {ref_lf:<20,}")

    # Verdict
    print("\n" + "="*80)
    if match_size and match_checksum:
        print("✓ VERDICT: Python script and raw serial capture are IDENTICAL")
        print("  → Corruption is NOT in Python script")
        print("  → Corruption is in FIRMWARE transmission")
    else:
        print("✗ VERDICT: Python script and raw serial capture are DIFFERENT")
        print("  → Corruption may be in Python script processing")
        print("  → Check decode/write operations")
    print("="*80)


def main():
    if len(sys.argv) < 3:
        print("Usage: python test_usb_transfer_integrity.py <port> <filename> [reference_file]")
        print()
        print("Example:")
        print("  python test_usb_transfer_integrity.py <port> 5kHz.csv sd_download/5kHz_copy.csv")
        sys.exit(1)

    port = sys.argv[1]
    filename = sys.argv[2]
    reference_file = sys.argv[3] if len(sys.argv) > 3 else None

    print("USB Transfer Integrity Test")
    print("="*80)
    print(f"Port: {port}")
    print(f"Filename: {filename}")
    if reference_file:
        print(f"Reference: {reference_file}")
    print("="*80)

    # Run tests
    result1 = test_python_download(port, filename)

    input("\nPress Enter to run raw serial capture (disconnect Python first)...")

    result2 = test_raw_serial_capture(port, filename)

    # Compare
    compare_results(result1, result2, reference_file)


if __name__ == "__main__":
    main()
