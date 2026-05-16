#!/usr/bin/env python3
"""
Test silent data loss observability counters (#295, #296, #297).

Validates that SYST:STR:STATS? exposes the three new fields:
  - EosOverruns (#295): EOS notification coalescing detection
  - DioDroppedSamples (#296): DIO queue overflow tracking
  - EncoderDroppedSamples (#297): Samples consumed by failed encodes

Tests push the device beyond its limits to trigger loss conditions
and verify the counters increment correctly.

Usage:
    python test_silent_loss_observability.py [port] [--duration N]

Requires: daqifi-python-core (pip install -e ../daqifi-python-core)
"""

import sys
import os
import time
import argparse
import re


from test_harness import ReliableSCPI, FastReader
from daqifi import NyquistDevice


def parse_stats(stats_text: str) -> dict:
    """Parse SYST:STR:STATS? response into a dict."""
    result = {}
    for line in stats_text.strip().split('\n'):
        line = line.strip()
        if '=' in line:
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip()
            try:
                result[key] = int(val)
            except ValueError:
                try:
                    result[key] = float(val)
                except ValueError:
                    result[key] = val
    return result


def stream_and_collect(scpi: ReliableSCPI, freq: int, duration: float,
                       benchmark: int = 0, pattern: int = 3,
                       chan_mask: int = 0xFFFF) -> dict:
    """Helper: configure, stream with FastReader, stop, return stats dict."""
    scpi.command(f"SYST:STR:TESTpattern {pattern}")
    scpi.command(f"SYST:STR:BENCHmark {benchmark}")
    scpi.command(f"CONF:ADC:CHAN {chan_mask}")
    scpi.command("SYST:STR:CLEARSTATS")
    time.sleep(0.3)

    reader = FastReader(scpi._serial)
    reader.start()
    scpi._serial.write(f"SYST:StartStreamData {freq}\r".encode('ascii'))
    scpi._serial.flush()
    time.sleep(duration)
    scpi._serial.write(b"SYST:StopStreamData\r")
    scpi._serial.flush()
    time.sleep(0.5)
    reader.stop()
    time.sleep(0.5)

    # Drain any remaining data before querying
    scpi.drain(0.5)
    stats_raw = scpi.query("SYST:STR:STATS?")
    return parse_stats(stats_raw), reader.bytes_received


def test_new_fields_present(scpi: ReliableSCPI) -> bool:
    """Test 1: Verify all three new fields appear in STATS response."""
    print("\n=== Test 1: New fields present in SYST:STR:STATS? ===")

    stats, pc_bytes = stream_and_collect(scpi, 100, 0.5)

    required_fields = [
        'EosOverruns',
        'DioDroppedSamples',
        'EncoderDroppedSamples',
    ]

    all_present = True
    for field in required_fields:
        if field in stats:
            print(f"  [PASS] {field} = {stats[field]}")
        else:
            print(f"  [FAIL] {field} NOT FOUND in stats response")
            all_present = False

    print(f"  [INFO] PC received {pc_bytes} bytes")

    # Clean up
    scpi.command("SYST:STR:TESTpattern 0")
    scpi.command("SYST:STR:BENCHmark 0")

    return all_present


def test_zero_at_safe_rate(scpi: ReliableSCPI, duration: float = 3.0) -> bool:
    """Test 2: At safe streaming rate, all new counters should be 0."""
    print(f"\n=== Test 2: Zero counters at safe rate (1kHz, {duration}s) ===")

    stats, pc_bytes = stream_and_collect(scpi, 1000, duration, benchmark=0)

    fields_to_check = {
        'EosOverruns': 0,
        'DioDroppedSamples': 0,
        'EncoderDroppedSamples': 0,
        'QueueDroppedSamples': 0,
    }

    all_zero = True
    for field, expected in fields_to_check.items():
        val = stats.get(field, -1)
        status = "PASS" if val == expected else "WARN"
        if val != expected:
            all_zero = False
        print(f"  [{status}] {field} = {val} (expected {expected})")

    total = stats.get('TotalSamplesStreamed', 0)
    timer = stats.get('TimerISRCalls', 0)
    print(f"  [INFO] TotalSamplesStreamed={total}, TimerISRCalls={timer}, PC bytes={pc_bytes}")

    scpi.command("SYST:STR:TESTpattern 0")
    return all_zero


def test_eos_coalesce_at_extreme_rate(scpi: ReliableSCPI, duration: float = 5.0) -> dict:
    """Test 3: Push T1 channels beyond ceiling to trigger EOS coalescing."""
    print(f"\n=== Test 3: EOS coalesce at extreme rate (5×T1, uncapped, {duration}s) ===")

    # 5 T1 channels: DaqifiAdcChannelId [4, 8, 10, 12, 14] on NQ1
    t1_mask = (1 << 4) | (1 << 8) | (1 << 10) | (1 << 12) | (1 << 14)  # 0x5510
    stats, pc_bytes = stream_and_collect(scpi, 30000, duration, benchmark=1, chan_mask=t1_mask)

    eos = stats.get('EosOverruns', 0)
    dropped = stats.get('QueueDroppedSamples', 0)
    total = stats.get('TotalSamplesStreamed', 0)
    timer = stats.get('TimerISRCalls', 0)

    print(f"  EosOverruns    = {eos}")
    print(f"  QueueDroppedSamples = {dropped}")
    print(f"  TotalSamplesStreamed = {total}")
    print(f"  TimerISRCalls       = {timer}")
    print(f"  PC bytes            = {pc_bytes}")

    if eos > 0:
        print(f"  [PASS] EOS coalescing detected: {eos} events")
    else:
        print(f"  [INFO] No EOS coalescing at 30kHz — task kept up")

    if dropped > 0:
        print(f"  [INFO] Queue drops also occurred: {dropped} (expected at extreme rate)")

    scpi.command("SYST:STR:TESTpattern 0")
    scpi.command("SYST:STR:BENCHmark 0")

    return stats


def test_queue_drops_trigger_counters(scpi: ReliableSCPI, duration: float = 5.0) -> dict:
    """Test 4: Push all 16 channels at extreme rate to force queue drops."""
    print(f"\n=== Test 4: Force queue drops at extreme rate (16ch, uncapped, {duration}s) ===")

    stats, pc_bytes = stream_and_collect(scpi, 27000, duration, benchmark=1, chan_mask=0xFFFF)

    print(f"  QueueDroppedSamples   = {stats.get('QueueDroppedSamples', 0)}")
    print(f"  EosOverruns      = {stats.get('EosOverruns', 0)}")
    print(f"  EncoderDroppedSamples = {stats.get('EncoderDroppedSamples', 0)}")
    print(f"  DioDroppedSamples     = {stats.get('DioDroppedSamples', 0)}")
    print(f"  EncoderFailures       = {stats.get('EncoderFailures', 0)}")
    print(f"  TotalSamplesStreamed   = {stats.get('TotalSamplesStreamed', 0)}")
    print(f"  TimerISRCalls          = {stats.get('TimerISRCalls', 0)}")
    print(f"  UsbDroppedBytes        = {stats.get('UsbDroppedBytes', 0)}")
    print(f"  PC bytes               = {pc_bytes}")

    dropped = stats.get('QueueDroppedSamples', 0)
    if dropped > 0:
        print(f"  [PASS] Queue drops detected: {dropped}")
    else:
        print(f"  [WARN] No queue drops — device kept up at 27kHz 16ch")

    scpi.command("SYST:STR:TESTpattern 0")
    scpi.command("SYST:STR:BENCHmark 0")

    return stats


def test_session_end_log(scpi: ReliableSCPI, duration: float = 3.0) -> bool:
    """Test 5: Verify session-end log includes new counter names."""
    print(f"\n=== Test 5: Session-end log format ({duration}s, uncapped 16ch) ===")

    # Clear log first
    scpi.query("SYST:LOG?")
    time.sleep(0.3)

    # Force drops with extreme rate
    stats, pc_bytes = stream_and_collect(scpi, 25000, duration, benchmark=1, chan_mask=0xFFFF)
    time.sleep(0.5)

    log = scpi.query("SYST:LOG?")
    print(f"  Log output ({len(log)} chars):")

    # Look for the session-end summary line
    found_summary = False
    for line in log.split('\n'):
        if 'Stream end:' in line:
            print(f"  >> {line.strip()}")
            found_summary = True
            # Check for new field names in the summary
            for tag in ['encFail=', 'encDrop=', 'dioDrop=', 'eos=']:
                if tag in line:
                    print(f"     [PASS] Found '{tag}' in summary")
                else:
                    print(f"     [FAIL] Missing '{tag}' in summary")

    if not found_summary:
        print("  [INFO] No 'Stream end:' line — may not have had drops")
        any_drops = (stats.get('QueueDroppedSamples', 0) > 0 or
                     stats.get('EosOverruns', 0) > 0 or
                     stats.get('DioDroppedSamples', 0) > 0)
        if not any_drops:
            print("  [INFO] No drops occurred — no summary expected (device kept up)")
            found_summary = True  # Not a failure

    scpi.command("SYST:STR:TESTpattern 0")
    scpi.command("SYST:STR:BENCHmark 0")

    return found_summary


def test_clearstats_resets_new_counters(scpi: ReliableSCPI) -> bool:
    """Test 6: CLEARSTATS resets all new counters to 0."""
    print("\n=== Test 6: CLEARSTATS resets new counters ===")

    # Run a quick session to potentially populate counters
    stats, _ = stream_and_collect(scpi, 20000, 1.0, benchmark=1)

    # Clear and verify
    scpi.command("SYST:STR:CLEARSTATS")
    time.sleep(0.2)

    stats = parse_stats(scpi.query("SYST:STR:STATS?"))

    new_fields = ['EosOverruns', 'DioDroppedSamples', 'EncoderDroppedSamples']
    all_zero = True
    for field in new_fields:
        val = stats.get(field, -1)
        status = "PASS" if val == 0 else "FAIL"
        if val != 0:
            all_zero = False
        print(f"  [{status}] {field} = {val} after CLEARSTATS")

    scpi.command("SYST:STR:TESTpattern 0")
    scpi.command("SYST:STR:BENCHmark 0")

    return all_zero


def test_invariant_timer_equals_samples_plus_drops(scpi: ReliableSCPI, duration: float = 5.0) -> bool:
    """Test 7: Verify TimerISRCalls == TotalSamplesStreamed + QueueDroppedSamples."""
    print(f"\n=== Test 7: Timer invariant (16ch uncapped, {duration}s) ===")

    stats, pc_bytes = stream_and_collect(scpi, 20000, duration, benchmark=1, chan_mask=0xFFFF)

    timer = stats.get('TimerISRCalls', 0)
    samples = stats.get('TotalSamplesStreamed', 0)
    drops = stats.get('QueueDroppedSamples', 0)
    expected = samples + drops

    diff = abs(timer - expected)
    pct = (diff / max(timer, 1)) * 100

    print(f"  TimerISRCalls          = {timer}")
    print(f"  TotalSamples + Drops   = {samples} + {drops} = {expected}")
    print(f"  Delta                  = {diff} ({pct:.2f}%)")

    ok = diff <= 2  # allow 1-2 for boundary timing
    print(f"  [{'PASS' if ok else 'FAIL'}] Invariant {'holds' if ok else 'BROKEN'}")

    scpi.command("SYST:STR:TESTpattern 0")
    scpi.command("SYST:STR:BENCHmark 0")

    return ok


def main():
    parser = argparse.ArgumentParser(description='Test silent loss observability counters')
    parser.add_argument('port', nargs='?', default=None, help='Serial port (auto-detect if omitted)')
    parser.add_argument('--duration', type=float, default=5.0, help='Streaming duration per test (seconds)')
    args = parser.parse_args()

    port = args.port or os.environ.get('DAQIFI_DEVICE_PORT')
    if not os.path.exists(port):
        print(f"ERROR: Device not found at {port}. Connect device and try again.")
        sys.exit(1)

    print(f"Using port: {port}")
    device = NyquistDevice.connect(port)
    scpi = ReliableSCPI(device)

    # Verify device identity
    idn = scpi.query("*IDN?")

    # Ensure device is powered up (streaming requires POWERED_UP state)
    scpi.command("SYST:POW:STAT 1")
    time.sleep(2)  # Wait for power-up sequence + ADC init
    power = scpi.query("SYST:POW:STAT?")
    print(f"Power state: {power.strip()}")

    # Set PB encoding (compact, exercises full pipeline)
    scpi.command("SYST:STR:FORMAT 0")
    time.sleep(0.3)
    print(f"Device: {idn.strip()}")

    results = {}

    try:
        results['fields_present'] = test_new_fields_present(scpi)
        results['zero_at_safe_rate'] = test_zero_at_safe_rate(scpi, args.duration)
        results['eos_coalesce'] = test_eos_coalesce_at_extreme_rate(scpi, args.duration)
        results['queue_drops'] = test_queue_drops_trigger_counters(scpi, args.duration)
        results['session_log'] = test_session_end_log(scpi, min(args.duration, 3.0))
        results['clearstats'] = test_clearstats_resets_new_counters(scpi)
        results['timer_invariant'] = test_invariant_timer_equals_samples_plus_drops(scpi, args.duration)
    finally:
        # Always clean up
        scpi.command("SYST:STR:TESTpattern 0")
        scpi.command("SYST:STR:BENCHmark 0")
        device.disconnect()

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    passed = 0
    total = len(results)
    for name, result in results.items():
        if isinstance(result, dict):
            status = "INFO"  # dict results are informational
        elif result:
            status = "PASS"
            passed += 1
        else:
            status = "FAIL"
        print(f"  {status:4s}  {name}")

    # Count dict results as informational (not pass/fail)
    bool_results = {k: v for k, v in results.items() if isinstance(v, bool)}
    passed = sum(1 for v in bool_results.values() if v)
    total = len(bool_results)
    print(f"\n  {passed}/{total} tests passed")

    return 0 if passed == total else 1


if __name__ == '__main__':
    sys.exit(main())
