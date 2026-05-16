#!/usr/bin/env python3
"""
Throughput benchmark for all interfaces, formats, and channel counts.

Uses benchmark mode (uncapped frequency) with test patterns.
Reports samples/sec, bytes/sec, and loss percentage.

Requires firmware with SYST:STR:BENCHmark support (PR #234+).

Usage:
    python test_throughput_benchmark.py <port>
    python test_throughput_benchmark.py <port> 15
"""

import sys
import time
import csv
import os
import re
from pathlib import Path
from datetime import datetime

from daqifi import NyquistDevice
from test_harness import ReliableSCPI, StreamingMeasurement


def _fmt_bytes(n):
    """Format byte count as human-readable string."""
    if n >= 1048576:
        return f"{n / 1048576:.1f}MB"
    elif n >= 1024:
        return f"{n / 1024:.0f}KB"
    else:
        return f"{n}B"


def run_benchmark(scpi, interface, fmt, channels, freq, duration_sec):
    """Run a single throughput benchmark with reliable stats collection."""
    fmt_names = {0: "PB", 1: "JSON", 2: "CSV"}
    iface_names = {0: "USB", 1: "WiFi", 2: "SD"}
    label = f"{iface_names[interface]} {fmt_names[fmt]} {len(channels)}ch@{freq}Hz"
    print(f"\n  Running: {label} for {duration_sec}s...", end='', flush=True)

    # === SETUP ===
    # Disable all channels
    scpi.command("ENABle:VOLTage:DC 0")  # bitmask=0 disables all
    time.sleep(0.2)
    for ch in channels:
        scpi.command(f"ENABle:VOLTage:DC {ch},1", delay=0.05)

    scpi.command(f"SYST:STR:FOR {fmt}")
    scpi.command(f"SYST:STR:INT {interface}")
    scpi.command("SYST:STR:TESTpattern 2")
    scpi.command("SYST:STR:BENCHmark 1")

    if interface == 2:  # SD
        scpi.command("SYST:STOR:SD:ENAble 1", delay=2.0)
        time.sleep(2)

    # Clear stats and drain THOROUGHLY before starting
    scpi.command("SYST:STR:ClearStats", delay=0.5)
    scpi.drain(0.5)

    # === STREAM ===
    scpi.command("SYST:MEM:AUTO", delay=0.5)  # Recompute buffer sizes for current interface config

    # Use StreamingMeasurement for precise PC-controlled timing
    m = StreamingMeasurement(scpi, scpi._serial)
    m.start(freq=freq, duration=duration_sec, is_csv=(fmt == 2))

    # === COLLECT STATS ===
    stats = m.fw_stats
    mem = scpi.query_kv("SYST:MEM:FREE?", delay=1.0)

    # === RESTORE ===
    scpi.command("SYST:STR:BENCHmark 0")
    scpi.command("SYST:STR:TESTpattern 0")

    # Verify we got valid stats
    total_samples = stats.get('TotalSamplesStreamed', 0)
    total_bytes = stats.get('TotalBytesStreamed', 0)
    queue_drops = stats.get('QueueDroppedSamples', 0)
    usb_drops = stats.get('UsbDroppedBytes', 0)
    sd_drops = stats.get('SdDroppedBytes', 0)
    enc_fails = stats.get('EncoderFailures', 0)
    total_attempted = total_samples + queue_drops

    sps = m.pc_sps
    bps = m.pc_kbps * 1024  # PC-measured bytes in window / window duration
    sample_loss_pct = (queue_drops * 100 / total_attempted) if total_attempted > 0 else 0

    # Byte loss: USB + SD dropped bytes vs total produced (encoded + dropped)
    total_byte_drops = usb_drops + sd_drops
    total_bytes_produced = total_bytes + total_byte_drops
    byte_loss_pct = (total_byte_drops * 100 / total_bytes_produced) if total_bytes_produced > 0 else 0

    pool_max = mem.get('SamplePoolMaxUsed', stats.get('SamplePoolMaxUsed', 0))

    # Identify pipeline bottleneck stage
    # Pipeline: ISR -> [Sample Queue] -> Encoder -> [USB buf / WiFi buf / SD buf] -> Host
    bottleneck = "none"
    if queue_drops > 0 and enc_fails == 0 and total_byte_drops == 0:
        bottleneck = "sample queue (ISR too fast for encoder)"
    elif enc_fails > 0 and total_byte_drops == 0:
        bottleneck = "encoder (encode failures)"
    elif usb_drops > 0 and sd_drops == 0:
        bottleneck = "USB output (host read too slow)"
    elif sd_drops > 0 and usb_drops == 0:
        bottleneck = "SD output (SPI write too slow)"
    elif usb_drops > 0 and sd_drops > 0:
        bottleneck = "USB+SD output (both overflowed)"
    elif queue_drops > 0 and total_byte_drops > 0:
        bottleneck = "multiple (queue + output)"
    elif queue_drops > 0:
        bottleneck = "sample queue"

    # SPS vs requested frequency sanity check
    sps_ratio = sps / freq if freq > 0 else 0
    has_drops = queue_drops > 0 or total_byte_drops > 0

    if sps_ratio >= 0.85 and sps_ratio <= 1.15:
        sps_flag = "OK"
    elif not has_drops:
        sps_flag = "CAPPED" if sps_ratio < 0.85 else "HIGH"
    else:
        sps_flag = "LOSS" if sps_ratio < 0.85 else "HIGH"

    result = {
        'interface': iface_names[interface],
        'format': fmt_names[fmt],
        'channels': len(channels),
        'freq_hz': freq,
        'duration_sec': round(m.window_duration, 2),
        'total_samples': total_samples,
        'total_bytes': total_bytes,
        'samples_per_sec': round(sps),
        'bytes_per_sec': round(bps),
        'kb_per_sec': round(m.pc_kbps, 1),
        'queue_drops': queue_drops,
        'usb_drops': usb_drops,
        'sd_drops': sd_drops,
        'encoder_fails': enc_fails,
        'sample_loss_pct': round(sample_loss_pct, 1),
        'byte_loss_pct': round(byte_loss_pct, 1),
        'bottleneck': bottleneck,
        'sps_ratio': round(sps_ratio, 2),
        'sps_flag': sps_flag,
        'pool_max_used': pool_max,
        'heap_min_free': mem.get('HeapMinEverFree', 0),
        'bytes_drained': m.pc_bytes_total,
        'valid': total_samples > 0 or total_attempted > 0,
    }

    if not result['valid']:
        print(f" INVALID (0 samples, 0 attempts — stats likely stale)")
    else:
        parts = []
        if sample_loss_pct > 0:
            parts.append(f"SAMPLE LOSS {sample_loss_pct:.1f}%")
        if byte_loss_pct > 0:
            parts.append(f"BYTE LOSS {byte_loss_pct:.1f}% [{bottleneck}]")
        if sps_flag not in ("OK",):
            parts.append(f"SPS {sps_flag} ({sps_ratio:.0%} of {freq}Hz)")

        if parts:
            print(f" {', '.join(parts)}: {result['samples_per_sec']} sps, "
                  f"{result['kb_per_sec']} KB/s")
        else:
            print(f" CLEAN: {result['samples_per_sec']} sps, {result['kb_per_sec']} KB/s")

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_throughput_benchmark.py <port> [duration_sec]")
        sys.exit(1)

    port = sys.argv[1]
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    print("=" * 70)
    print("  DAQiFi Throughput Benchmark (Reliable)")
    print(f"  Port: {port}, Duration: {duration}s per test")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    device = NyquistDevice.connect(port)
    scpi = ReliableSCPI(device)

    # Power up (ignore errors if already on)
    try:
        device.power_up()
    except:
        scpi.command("SYST:POW:STAT 1", delay=3.0)
    time.sleep(2)

    print(f"\n  Device: {device}")
    print(f"  Firmware: {device.firmware_version}")
    mem = scpi.query_kv("SYST:MEM:FREE?")
    print(f"  Heap: {mem.get('HeapFree', '?')} free, Pool: {mem.get('SamplePoolCount', '?')}")

    all_ch = list(range(16))

    tests = [
        # (interface, format, channels, freq)
        # USB PB sweep
        (0, 0, [0],     1000),
        (0, 0, [0],     5000),
        (0, 0, [0],     11000),
        (0, 0, all_ch,  1000),
        (0, 0, all_ch,  3000),
        (0, 0, all_ch,  5000),
        # USB CSV sweep
        (0, 2, [0],     5000),
        (0, 2, [0],     11000),
        (0, 2, [0],     15000),
        (0, 2, all_ch,  1000),
        (0, 2, all_ch,  3000),
        # SD PB sweep
        (2, 0, [0],     5000),
        (2, 0, all_ch,  1000),
        (2, 0, all_ch,  3000),
        # SD CSV sweep
        (2, 2, [0],     5000),
        (2, 2, all_ch,  1000),
    ]

    results = []
    for interface, fmt, channels, freq in tests:
        try:
            r = run_benchmark(scpi, interface, fmt, channels, freq, duration)
            results.append(r)
        except Exception as e:
            print(f" ERROR: {e}")

    # Summary
    print("\n" + "=" * 85)
    print("  RESULTS")
    print("=" * 85)
    print(f"\n  {'Config':<30s} {'Freq':>6s} {'SPS':>7s} {'Ratio':>6s} {'KB/s':>6s} "
          f"{'Samp%':>6s} {'Byte%':>6s} {'USB Drop':>9s} {'SD Drop':>8s} {'Bottleneck'}")
    print("  " + "-" * 105)
    for r in results:
        label = f"{r['interface']} {r['format']} {r['channels']}ch"
        freq_s = f"{r['freq_hz']}"
        ratio = r['sps_ratio']
        ratio_s = f"{ratio:.0%}" if r['sps_flag'] == 'OK' else f"{ratio:.0%}!"
        s_loss = f"{r['sample_loss_pct']}%" if r['sample_loss_pct'] > 0 else "-"
        b_loss = f"{r['byte_loss_pct']}%" if r['byte_loss_pct'] > 0 else "-"
        usb_dr = _fmt_bytes(r['usb_drops']) if r['usb_drops'] > 0 else "-"
        sd_dr = _fmt_bytes(r['sd_drops']) if r['sd_drops'] > 0 else "-"
        bn = r['bottleneck'] if r['bottleneck'] != 'none' else "-"
        valid_mark = "" if r['valid'] else " [!]"
        print(f"  {label:<30s} {freq_s:>6s} {r['samples_per_sec']:>7d} {ratio_s:>6s} "
              f"{r['kb_per_sec']:>5.1f} {s_loss:>6s} {b_loss:>6s} {usb_dr:>9s} {sd_dr:>8s}  "
              f"{bn}{valid_mark}")

    # Legend
    print()
    print("  Pipeline: ISR -> [Sample Queue] -> Encoder -> [USB/WiFi/SD buf] -> Host")
    print("  Ratio = SPS/Freq (100%=matched, <85%=capped/dropping, >115%=overshoot)")
    print("  Samp% = queue overflow | Byte% = output overflow | Bottleneck = limiting stage")

    # Flag issues
    invalid = [r for r in results if not r['valid']]
    if invalid:
        print(f"\n  WARNING: {len(invalid)} tests returned invalid data (0 samples)")
        for r in invalid:
            print(f"    {r['interface']} {r['format']} {r['channels']}ch@{r['freq_hz']}Hz")

    byte_loss_tests = [r for r in results if r['byte_loss_pct'] > 0]
    if byte_loss_tests:
        print(f"\n  NOTE: {len(byte_loss_tests)} tests had output byte loss (data produced faster "
              f"than host could drain):"  )
        for r in byte_loss_tests:
            label = f"{r['interface']} {r['format']} {r['channels']}ch@{r['freq_hz']}Hz"
            total_produced = r['total_bytes'] + r['usb_drops'] + r['sd_drops']
            print(f"    {label}: {r['byte_loss_pct']}% byte loss "
                  f"({_fmt_bytes(r['usb_drops'] + r['sd_drops'])} of "
                  f"{_fmt_bytes(total_produced)} dropped)")

    sps_issues = [r for r in results if r.get('sps_flag') not in ('OK', None)]
    if sps_issues:
        print(f"\n  SPS SANITY: {len(sps_issues)} tests where SPS didn't match requested freq:")
        for r in sps_issues:
            label = f"{r['interface']} {r['format']} {r['channels']}ch@{r['freq_hz']}Hz"
            print(f"    {label}: {r['sps_flag']} — got {r['samples_per_sec']} sps "
                  f"({r['sps_ratio']:.0%} of {r['freq_hz']}Hz)")

    # Save
    csv_file = f"benchmark_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    valid_results = [r for r in results if r['valid']]
    if valid_results:
        keys = [k for k in valid_results[0].keys()]
        with open(csv_file, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(valid_results)
        print(f"\n  Saved to {csv_file}")

    device.disconnect()
    print("\n  Done!")


if __name__ == "__main__":
    main()
