#!/usr/bin/env python3
"""
Bottleneck analysis: push each config to failure, identify limiting factor,
then test with doubled buffer to see if it helps.

For each interface×format×channels:
  1. Binary search for the zero-loss ceiling
  2. Report bottleneck (sample queue, USB drain, SD write, encoder)
  3. Log the buffer sizes at failure point
"""

import sys
import time
from pathlib import Path
from datetime import datetime

from daqifi import NyquistDevice
from test_harness import ReliableSCPI, StreamingMeasurement


def run_test(scpi, interface, fmt, channels, freq, duration=10):
    """Run a single test. Returns stats dict."""
    scpi.command("ENABle:VOLTage:DC 0")
    time.sleep(0.2)
    for ch in channels:
        scpi.command(f"ENABle:VOLTage:DC {ch},1", delay=0.05)

    scpi.command(f"SYST:STR:FOR {fmt}")
    scpi.command("SYST:STR:TESTpattern 2")
    scpi.command("SYST:STR:BENCHmark 1")  # Bypass freq cap to find true ceiling

    # Ensure SD is in the right state for this test
    if interface == 2:
        scpi.command("SYST:STOR:SD:ENAble 1", delay=3.0)
        time.sleep(2)
    else:
        scpi.command("SYST:STOR:SD:ENAble 0", delay=1.0)

    scpi.command(f"SYST:STR:INT {interface}")

    # Recompute buffer sizes for current interface config
    scpi.command("SYST:MEM:AUTO", delay=0.5)

    scpi.command("SYST:STR:ClearStats", delay=0.5)
    scpi.drain(0.5)

    # Use StreamingMeasurement for precise PC-controlled timing
    m = StreamingMeasurement(scpi, scpi._serial)
    m.start(freq=freq, duration=duration, is_csv=(fmt == 2))

    scpi.command("SYST:STR:BENCHmark 0")
    scpi.command("SYST:STR:TESTpattern 0")

    stats = m.fw_stats

    mem = scpi.query_kv("SYST:MEM:FREE?", delay=1.0)

    if interface == 2:
        scpi.command("SYST:STOR:SD:ENAble 0", delay=2.0)
        time.sleep(1)

    stats['_mem'] = mem
    stats['_pc_sps'] = m.pc_sps
    stats['_pc_kbps'] = m.pc_kbps
    return stats


def analyze_bottleneck(stats):
    """Determine what caused loss."""
    q_drops = stats.get('QueueDroppedSamples', 0)
    usb_drops = stats.get('UsbDroppedBytes', 0)
    wifi_drops = stats.get('WifiDroppedBytes', 0)
    sd_drops = stats.get('SdDroppedBytes', 0)
    enc_fails = stats.get('EncoderFailures', 0)
    total = stats.get('TotalSamplesStreamed', 0)
    attempted = total + q_drops

    if attempted == 0:
        return "INVALID", 0

    loss_pct = q_drops * 100 / attempted if attempted > 0 else 0

    if loss_pct == 0 and usb_drops == 0 and wifi_drops == 0 and sd_drops == 0:
        return "PASS", 0

    bottleneck = []
    if q_drops > 0:
        bottleneck.append(f"SampleQueue({q_drops})")
    if usb_drops > 0:
        bottleneck.append(f"UsbDrain({usb_drops}B)")
    if sd_drops > 0:
        bottleneck.append(f"SdWrite({sd_drops}B)")
    if wifi_drops > 0:
        bottleneck.append(f"WifiSend({wifi_drops}B)")
    if enc_fails > 0:
        bottleneck.append(f"Encoder({enc_fails})")

    return "+".join(bottleneck) if bottleneck else "Unknown", loss_pct


def find_ceiling(scpi, interface, fmt, channels, label, duration=10):
    """Binary search for zero-loss ceiling frequency."""
    fmt_names = {0: "PB", 2: "CSV"}
    iface_names = {0: "USB", 2: "SD"}

    # Start with known-good rate, double until failure
    freq = 500
    last_pass = 0
    first_fail = None

    print(f"\n  {label}")
    print(f"  {'Rate':>8s}  {'SPS':>7s}  {'Loss':>6s}  {'Bottleneck':<40s}  {'Pool':>5s}")
    print(f"  {'-'*75}")

    # Ramp up
    while freq <= 30000:
        stats = run_test(scpi, interface, fmt, channels, freq, duration)
        bottleneck, loss = analyze_bottleneck(stats)
        sps = stats.get('_pc_sps', 0)
        pool_max = stats.get('_mem', {}).get('SamplePoolMaxUsed', 0)

        if loss == 0 and "PASS" in bottleneck:
            print(f"  {freq:>7d}   {sps:>7.0f}    0%   PASS{' ':36s}  {pool_max:>5d}")
            last_pass = freq
            freq = int(freq * 1.5)  # Ramp faster
        else:
            print(f"  {freq:>7d}   {sps:>7.0f}  {loss:>5.1f}%  {bottleneck:<40s}  {pool_max:>5d}")
            first_fail = freq
            break

    if first_fail is None:
        print(f"  >>> Ceiling: >{last_pass} Hz (didn't reach failure)")
        return last_pass, "CPU/ISR"

    # Binary search between last_pass and first_fail
    lo, hi = last_pass, first_fail
    ceiling = last_pass
    final_bottleneck = "Unknown"

    while hi - lo > 200:
        mid = (lo + hi) // 2
        stats = run_test(scpi, interface, fmt, channels, mid, duration)
        bottleneck, loss = analyze_bottleneck(stats)
        sps = stats.get('_pc_sps', 0)
        pool_max = stats.get('_mem', {}).get('SamplePoolMaxUsed', 0)

        if loss == 0 and "PASS" in bottleneck:
            print(f"  {mid:>7d}   {sps:>7.0f}    0%   PASS{' ':36s}  {pool_max:>5d}")
            lo = mid
            ceiling = mid
        else:
            print(f"  {mid:>7d}   {sps:>7.0f}  {loss:>5.1f}%  {bottleneck:<40s}  {pool_max:>5d}")
            hi = mid
            final_bottleneck = bottleneck

    print(f"  >>> Ceiling: {ceiling} Hz | Bottleneck: {final_bottleneck}")
    return ceiling, final_bottleneck


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyACM0'
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    print("=" * 80)
    print("  Bottleneck Analysis — Find Ceilings & Limiting Factors")
    print(f"  Port: {port}, Duration: {duration}s per test")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 80)

    device = NyquistDevice.connect(port)
    scpi = ReliableSCPI(device)

    try:
        device.power_up()
    except:
        scpi.command("SYST:POW:STAT 1", delay=3.0)
    time.sleep(2)

    mem = scpi.query_kv("SYST:MEM:FREE?")
    print(f"  Pool: {mem.get('SamplePoolCount', '?')}, Heap: {mem.get('HeapFree', '?')}")
    print(f"  SdCircular: {mem.get('SdCircularSize', '?')}")

    ch1 = [0]
    ch8 = list(range(8))
    ch16 = list(range(16))

    results = []

    configs = [
        (0, 0, ch1,  "USB PB  1ch"),
        (0, 0, ch8,  "USB PB  8ch"),
        (0, 0, ch16, "USB PB 16ch"),
        (0, 2, ch1,  "USB CSV  1ch"),
        (0, 2, ch8,  "USB CSV  8ch"),
        (0, 2, ch16, "USB CSV 16ch"),
        (2, 0, ch1,  "SD PB  1ch"),
        (2, 0, ch8,  "SD PB  8ch"),
        (2, 0, ch16, "SD PB 16ch"),
        (2, 2, ch1,  "SD CSV  1ch"),
        (2, 2, ch8,  "SD CSV  8ch"),
        (2, 2, ch16, "SD CSV 16ch"),
    ]

    for iface, fmt, chs, label in configs:
        ceiling, bottleneck = find_ceiling(scpi, iface, fmt, chs, label, duration)
        results.append((label, ceiling, bottleneck))

    print(f"\n{'='*80}")
    print(f"  CEILING SUMMARY")
    print(f"{'='*80}")
    print(f"  {'Config':<20s}  {'Ceiling':>8s}  {'Bottleneck':<40s}")
    print(f"  {'-'*70}")
    for label, ceiling, bottleneck in results:
        print(f"  {label:<20s}  {ceiling:>7d}   {bottleneck}")

    device.disconnect()
    print("\n  Done!")


if __name__ == "__main__":
    main()
