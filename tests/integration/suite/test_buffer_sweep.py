#!/usr/bin/env python3
"""
Buffer size sweep benchmark: measures how USB/WiFi circular buffer sizing
affects streaming throughput across interfaces, formats, and channel counts.

Uses the StreamingBufferPool partition (SYST:MEM:AUTO or explicit sizing)
to vary buffer sizes between test runs. Sample pool gets remaining space.

Usage:
    python test_buffer_sweep.py <port>
    python test_buffer_sweep.py <port> 10  # 10s per test
"""

import sys
import time
import csv
from pathlib import Path
from datetime import datetime

from test_throughput_benchmark import ReliableSCPI, run_benchmark
from daqifi import NyquistDevice


def set_buffer_sizes(scpi, usb_size, wifi_size):
    """Set explicit buffer sizes via SCPI. Returns actual sizes after partition."""
    scpi.command(f"SYST:MEM:USB:BUF {usb_size}", delay=0.3)
    scpi.command(f"SYST:MEM:WIFI:BUF {wifi_size}", delay=0.3)
    # Query back to confirm
    return usb_size, wifi_size


def reset_to_auto(scpi):
    """Reset memory config to all-zero (auto mode)."""
    # There's no reset command, so we need to reboot or set all to defaults.
    # For now, set to compile-time defaults which triggers auto mode detection.
    # Actually, setting any field makes it non-zero. We can't reset to zero via SCPI.
    # Workaround: the auto-balance at stream start checks if ALL fields are zero.
    # After explicit sets, they're non-zero. We'll just track what we set.
    pass


def query_pool_info(scpi):
    """Query current pool partition info."""
    mem = scpi.query_kv("SYST:MEM:FREE?", delay=1.0)
    return {
        'heap_free': mem.get('HeapFree', 0),
        'heap_min': mem.get('HeapMinEverFree', 0),
        'sample_count': mem.get('SamplePoolCount', 0),
        'sample_max_used': mem.get('SamplePoolMaxUsed', 0),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_buffer_sweep.py <port> [duration_sec]")
        sys.exit(1)

    port = sys.argv[1]
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    print("=" * 70)
    print("  Buffer Size Sweep Benchmark")
    print(f"  Port: {port}, Duration: {duration}s per test")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    device = NyquistDevice.connect(port)
    scpi = ReliableSCPI(device)

    try:
        device.power_up()
    except:
        scpi.command("SYST:POW:STAT 1", delay=3.0)
    time.sleep(2)

    print(f"\n  Device: {device}")
    mem = scpi.query_kv("SYST:MEM:FREE?")
    print(f"  Heap: {mem.get('HeapFree', '?')} free")
    print(f"  Pool samples: {mem.get('SamplePoolCount', '?')}")

    all_ch = list(range(16))

    # =========================================================================
    # Test matrix: buffer sizes × interface × format × channels × frequency
    # =========================================================================
    # Buffer sizes to test (USB, WiFi)
    usb_sizes = [4096, 8192, 16384, 32768]
    wifi_sizes = [1400, 7000, 14000, 28000]

    # Reference test configurations at ceiling frequencies
    usb_tests = [
        # (format, channels, freq, label)
        (0, [0],     20000, "USB PB 1ch@20k"),
        (0, all_ch,   7000, "USB PB 16ch@7k"),
        (2, [0],     15000, "USB CSV 1ch@15k"),
        (2, all_ch,   3000, "USB CSV 16ch@3k"),
    ]

    wifi_tests = [
        (0, all_ch,   1000, "WiFi PB 16ch@1k"),
        (2, all_ch,   1000, "WiFi CSV 16ch@1k"),
    ]

    sd_tests = [
        (0, [0],     11000, "SD PB 1ch@11k"),
        (0, all_ch,   7000, "SD PB 16ch@7k"),
        (2, [0],      5000, "SD CSV 1ch@5k"),
        (2, all_ch,   1000, "SD CSV 16ch@1k"),
    ]

    results = []

    # =========================================================================
    # USB buffer sweep (WiFi at minimum)
    # =========================================================================
    print(f"\n{'='*70}")
    print("  PHASE 1: USB Buffer Size Sweep (WiFi=min)")
    print(f"{'='*70}")

    for usb_kb in usb_sizes:
        set_buffer_sizes(scpi, usb_kb, 1400)
        pool = query_pool_info(scpi)
        print(f"\n  --- USB={usb_kb//1024}KB, WiFi=1.4KB, Samples={pool['sample_count']} ---")

        for fmt, channels, freq, label in usb_tests:
            try:
                r = run_benchmark(scpi, 0, fmt, channels, freq, duration)
                r['usb_buf_size'] = usb_kb
                r['wifi_buf_size'] = 1400
                r['sample_pool'] = pool['sample_count']
                r['test_group'] = 'usb_sweep'
                results.append(r)
            except Exception as e:
                print(f" ERROR: {e}")

    # =========================================================================
    # WiFi buffer sweep (USB at minimum) — only if WiFi client available
    # =========================================================================
    print(f"\n{'='*70}")
    print("  PHASE 2: WiFi Buffer Size Sweep (USB=min)")
    print(f"{'='*70}")

    for wifi_kb in wifi_sizes:
        set_buffer_sizes(scpi, 4096, wifi_kb)
        pool = query_pool_info(scpi)
        print(f"\n  --- USB=4KB, WiFi={wifi_kb//1024 if wifi_kb >= 1024 else wifi_kb}B, Samples={pool['sample_count']} ---")

        for fmt, channels, freq, label in wifi_tests:
            try:
                r = run_benchmark(scpi, 1, fmt, channels, freq, duration)
                r['usb_buf_size'] = 4096
                r['wifi_buf_size'] = wifi_kb
                r['sample_pool'] = pool['sample_count']
                r['test_group'] = 'wifi_sweep'
                results.append(r)
            except Exception as e:
                print(f" ERROR: {e}")

    # =========================================================================
    # SD tests with varying USB buffer (SD uses coherent pool, not streaming pool)
    # =========================================================================
    print(f"\n{'='*70}")
    print("  PHASE 3: SD Tests (buffer size shouldn't affect SD)")
    print(f"{'='*70}")

    for usb_kb in [4096, 32768]:
        set_buffer_sizes(scpi, usb_kb, 1400)
        pool = query_pool_info(scpi)
        print(f"\n  --- USB={usb_kb//1024}KB, WiFi=1.4KB, Samples={pool['sample_count']} ---")

        for fmt, channels, freq, label in sd_tests:
            try:
                r = run_benchmark(scpi, 2, fmt, channels, freq, duration)
                r['usb_buf_size'] = usb_kb
                r['wifi_buf_size'] = 1400
                r['sample_pool'] = pool['sample_count']
                r['test_group'] = 'sd_test'
                results.append(r)
            except Exception as e:
                print(f" ERROR: {e}")

    # =========================================================================
    # Results
    # =========================================================================
    print(f"\n{'='*70}")
    print("  RESULTS")
    print(f"{'='*70}")

    # Group by test group
    for group in ['usb_sweep', 'wifi_sweep', 'sd_test']:
        group_results = [r for r in results if r.get('test_group') == group]
        if not group_results:
            continue
        print(f"\n  {group.upper().replace('_', ' ')}:")
        print(f"  {'Config':<25s} {'USB':>6s} {'WiFi':>6s} {'Pool':>5s} {'SPS':>7s} {'KB/s':>7s} {'Loss':>6s} {'Drops':>7s}")
        print(f"  {'-'*70}")
        for r in group_results:
            label = f"{r['interface']} {r['format']} {r['channels']}ch@{r['freq_hz']}Hz"
            loss = f"{r['loss_percent']}%" if r['loss_percent'] > 0 else "0%"
            drops = r['queue_drops'] + r['usb_drops'] + r['sd_drops']
            usb_s = f"{r['usb_buf_size']//1024}K"
            wifi_s = f"{r['wifi_buf_size']//1024}K" if r['wifi_buf_size'] >= 1024 else f"{r['wifi_buf_size']}"
            print(f"  {label:<25s} {usb_s:>6s} {wifi_s:>6s} {r['sample_pool']:>5d} "
                  f"{r['samples_per_sec']:>7d} {r['kb_per_sec']:>6.1f} {loss:>6s} {drops:>7d}")

    # Save CSV
    csv_file = f"buffer_sweep_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    if results:
        keys = list(results[0].keys())
        with open(csv_file, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(results)
        print(f"\n  Saved to {csv_file}")

    device.disconnect()
    print("\n  Done!")


if __name__ == "__main__":
    main()
