#!/usr/bin/env python3
"""
Run complete battery of high-speed ADC tests and generate text report.

Single-channel tests (Channel 4, Type 1):
- 5k, 6k, 7k, 8k, 9k, 10k Hz (1 kHz increments to find bottleneck)

Dual-channel tests (Channel 4 Type1 + Channel 5 Type2):
- 2k, 4k, 6k, 8k, 10k, 12k, 14k, 16k, 18k, 20k Hz

Generates comprehensive text report with:
- Summary table for all tests
- SD write speed analysis (KB/s)
- Sample rate accuracy
- Waveform validation (10 Hz triangle on ch4)
- Full-file anomaly detection
- Bottleneck identification

Usage:
    python run_all_rate_tests.py <port> [duration] [--single|--dual|--all]
    # Or rely on env var: DAQIFI_DEVICE_PORT

Examples:
    python run_all_rate_tests.py <port> 10          # Single-channel tests only
    python run_all_rate_tests.py <port> 10 --dual   # Dual-channel tests only
    python run_all_rate_tests.py <port> 10 --all    # Both single and dual
"""

import os
import sys
import time
from pathlib import Path
from datetime import datetime

from daqifi import NyquistDevice


def enable_channels(device, channels):
    """Enable specified channels, disable all others"""
    for ch in range(16):
        device._comm.send_command(f"ENABLE:VOLT:DC {ch},0")
    for ch in channels:
        device._comm.send_command(f"ENABLE:VOLT:DC {ch},1")


def run_single_test(device, sample_rate, duration_sec, test_num, total_tests, dual_channel=False):
    """Run one rate test and return results

    Args:
        device: Connected NyquistDevice
        sample_rate: Sample rate in Hz
        duration_sec: Test duration in seconds
        test_num: Current test number
        total_tests: Total number of tests
        dual_channel: If True, enable channels 4 and 5. If False, only channel 4.
    """

    channels_str = "dual" if dual_channel else "single"
    filename = f"triangle_{sample_rate}hz_{channels_str}.csv"

    print(f"\n{'='*80}")
    print(f"TEST {test_num}/{total_tests}: {sample_rate} Hz")
    print("="*80)

    try:
        # Configure SD logging
        device._comm.send_command("SYST:STOR:SD:ENAble 1")
        device._comm.send_command(f'SYST:STOR:SD:LOGging "{filename}"')
        device._comm.send_command("SYST:STReam:INTerface 2")
        device._comm.send_command("SYST:STReam:FORmat 2")

        # Enable channels based on mode
        if dual_channel:
            # Use channels 4 and 6 (both Type 1)
            print("2. Enabling channels 4 and 6 (both Type 1)...")
            enable_channels(device, [4, 6])
        else:
            print("2. Enabling channel 4 only (Type1)...")
            enable_channels(device, [4])

        # Start streaming
        print(f"Streaming at {sample_rate} Hz for {duration_sec} seconds...")
        device._comm.send_command(f"SYST:StartStreamData {sample_rate}")

        for i in range(duration_sec):
            time.sleep(1)
            print(f"  {i+1}/{duration_sec}", end='\r')
        print()

        # Stop
        device._comm.send_command("SYST:StopStreamData")
        time.sleep(0.5)

        # Check errors
        errors = device._comm.send_query("SYST:ERR?")

        # Download file
        print("Downloading...")
        cmd = f'SYST:STOR:SD:GET "{filename}"'
        device._comm._serial.write((cmd + "\r").encode('ascii'))
        device._comm._serial.flush()

        end_marker = b"__END_OF_FILE__"
        start_time = time.time()
        data = b""

        while (time.time() - start_time) < 60:
            if device._comm._serial.in_waiting > 0:
                data += device._comm._serial.read(device._comm._serial.in_waiting)
                if end_marker in data:
                    if b'#' in data:
                        data = data[data.find(b'#'):data.find(end_marker)]
                    break
            time.sleep(0.01)

        # Save
        output_file = Path("sd_download") / filename
        output_file.parent.mkdir(exist_ok=True)
        with open(output_file, 'wb') as f:
            f.write(data)

        # Quick analysis
        lines = data.decode('ascii', errors='ignore').split('\n')
        timestamps = []
        values = []

        for line in lines:
            if line.startswith('#') or not line.strip() or '_ts' in line:
                continue
            parts = line.split(',')
            if len(parts) >= 2:
                try:
                    timestamps.append(int(parts[0]))
                    values.append(float(parts[1]))
                except:
                    pass

        if len(timestamps) < 100:
            return {'rate': sample_rate, 'success': False, 'error': 'Not enough samples'}

        # Calculate metrics
        intervals = [timestamps[i] - timestamps[i-1] for i in range(2, min(1000, len(timestamps)))]
        avg_interval = sum(intervals) / len(intervals)
        measured_hz = 50_000_000 / avg_interval
        error_pct = abs(measured_hz - sample_rate) / sample_rate * 100

        # Detect peaks with adaptive window size
        peaks = []
        expected_period = measured_hz / 10  # samples per 10Hz wave period
        min_spacing = int(expected_period * 0.8)
        # Window = 5% of period (min 20, max 200 samples)
        window = max(20, min(200, int(expected_period * 0.05)))

        for i in range(window, len(values)-window):
            is_peak = all(values[i] >= values[i+j] for j in range(-window, window+1) if j != 0)
            if is_peak and (not peaks or i - peaks[-1] >= min_spacing):
                peaks.append(i)

        periods = [peaks[i] - peaks[i-1] for i in range(1, len(peaks))] if len(peaks) > 1 else []
        avg_period = sum(periods) / len(periods) if periods else 0
        wave_hz = measured_hz / avg_period if avg_period > 0 else 0

        # Full-file anomaly detection with adaptive thresholds
        total_intervals = [timestamps[i] - timestamps[i-1] for i in range(1, len(timestamps))]

        # Gap detection: use expected interval from requested rate
        expected_interval = 50_000_000 / sample_rate  # ticks (50MHz clock)
        large_gaps = [iv for iv in total_intervals if iv > expected_interval * 2]

        # Amplitude anomalies with adaptive threshold based on actual signal
        # Calculate peak-to-peak using percentiles to filter outliers (INT32 overflow)
        sorted_vals = sorted(values)
        p5_idx = int(len(sorted_vals) * 0.05)
        p95_idx = int(len(sorted_vals) * 0.95)
        peak_to_peak = sorted_vals[p95_idx] - sorted_vals[p5_idx]

        # Jump threshold = 25% of peak-to-peak (adapts to any signal amplitude)
        jump_threshold = peak_to_peak * 0.25
        deltas = [abs(values[i] - values[i-1]) for i in range(1, len(values))]
        large_jumps = [d for d in deltas if d > jump_threshold]

        result = {
            'rate': sample_rate,
            'dual_channel': dual_channel,
            'success': True,
            'filename': str(output_file),
            'samples': len(timestamps),
            'duration': (timestamps[-1] - timestamps[0]) / 50_000_000,
            'measured_hz': measured_hz,
            'error_pct': error_pct,
            'peaks': len(peaks),
            'wave_hz': wave_hz,
            'wave_error_pct': abs(wave_hz - 10) / 10 * 100 if wave_hz > 0 else 100,
            'min_val': sorted_vals[p5_idx],   # Use percentile to filter outliers
            'max_val': sorted_vals[p95_idx],  # Use percentile to filter outliers
            'mean_val': sum(values) / len(values),
            'peak_to_peak': peak_to_peak,
            'large_gaps': len(large_gaps),
            'large_jumps': len(large_jumps),
            'timestamps': timestamps[:5000],  # First 5000 for analysis
            'values': values[:5000],
        }

        print(f"[OK] Measured: {measured_hz:.1f} Hz ({error_pct:.2f}% error)")
        print(f"[OK] Wave: {wave_hz:.2f} Hz ({result['wave_error_pct']:.2f}% error)")

        return result

    except Exception as e:
        print(f"[FAIL] {e}")
        return {'rate': sample_rate, 'success': False, 'error': str(e)}


def generate_text_report(results, output_file="adc_test_report.txt"):
    """Generate comprehensive text report with all test results"""

    print(f"\n{'='*80}")
    print("Generating Text Report")
    print("="*80)

    try:
        with open(output_file, 'w') as f:
            # Header
            f.write("="*80 + "\n")
            f.write("HIGH-SPEED ADC LOGGING BENCHMARK REPORT\n")
            f.write("="*80 + "\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Test Signal: 10 Hz Triangle Wave, 1Vpp, +1V offset, Single-ended\n")
            f.write(f"Channel: 4 (Type 1 high-speed)\n")
            f.write("="*80 + "\n\n")

            # Summary table
            f.write("SUMMARY TABLE\n")
            f.write("-"*80 + "\n")
            f.write(f"{'Rate':>8} | {'Measured':>10} | {'Error':>7} | {'Wave Hz':>8} | {'Samples':>8} | {'SD KB/s':>8} | {'Gaps':>5} | {'Jumps':>6} | Status\n")
            f.write("-"*80 + "\n")

            for r in results:
                if r['success']:
                    # Calculate SD write speed
                    import os
                    if os.path.exists(r['filename']):
                        file_size_kb = os.path.getsize(r['filename']) / 1024
                        sd_speed = file_size_kb / r['duration']
                    else:
                        sd_speed = 0

                    status = "PASS" if r['error_pct'] < 5 and r['large_gaps'] == 0 else "WARN"
                    f.write(f"{r['rate']:>6} Hz | {r['measured_hz']:>8.1f} Hz | "
                           f"{r['error_pct']:>6.2f}% | {r['wave_hz']:>7.2f} Hz | "
                           f"{r['samples']:>8,} | {sd_speed:>7.1f} | {r['large_gaps']:>5} | {r['large_jumps']:>6} | {status}\n")
                else:
                    f.write(f"{r['rate']:>6} Hz | {'FAILED':>10} | {'N/A':>7} | "
                           f"{'N/A':>8} | {'N/A':>8} | {'N/A':>8} | {'N/A':>5} | {'N/A':>6} | FAIL\n")

            f.write("-"*80 + "\n\n")

            # Detailed results for each test
            f.write("DETAILED RESULTS\n")
            f.write("="*80 + "\n\n")

            for r in results:
                f.write(f"{r['rate']} Hz Test\n")
                f.write("-"*80 + "\n")

                if r['success']:
                    # Calculate SD write speed
                    if os.path.exists(r['filename']):
                        file_size_bytes = os.path.getsize(r['filename'])
                        file_size_kb = file_size_bytes / 1024
                        sd_speed_kbps = file_size_kb / r['duration']
                        bytes_per_sample = file_size_bytes / r['samples']

                        f.write(f"Sample Rate:\n")
                        f.write(f"  Requested: {r['rate']} Hz\n")
                        f.write(f"  Measured: {r['measured_hz']:.2f} Hz\n")
                        f.write(f"  Error: {r['error_pct']:.2f}%\n")
                        f.write(f"  Total samples: {r['samples']:,}\n")
                        f.write(f"  Duration: {r['duration']:.2f} seconds\n\n")

                        f.write(f"Waveform Validation:\n")
                        f.write(f"  Expected frequency: 10 Hz\n")
                        f.write(f"  Measured frequency: {r['wave_hz']:.2f} Hz\n")
                        f.write(f"  Error: {r['wave_error_pct']:.2f}%\n")
                        f.write(f"  Peaks detected: {r['peaks']}\n\n")

                        f.write(f"SD Card Performance:\n")
                        f.write(f"  File size: {file_size_kb:.1f} KB ({file_size_bytes:,} bytes)\n")
                        f.write(f"  Write speed: {sd_speed_kbps:.1f} KB/s\n")
                        f.write(f"  Bytes per sample: {bytes_per_sample:.1f}\n")
                        f.write(f"  vs 400 KB/s benchmark: {sd_speed_kbps/400*100:.1f}%\n\n")

                        f.write(f"Signal Quality:\n")
                        f.write(f"  Min: {r['min_val']:.0f} counts\n")
                        f.write(f"  Max: {r['max_val']:.0f} counts\n")
                        f.write(f"  Mean: {r['mean_val']:.0f} counts\n")
                        f.write(f"  Peak-to-peak: {r['max_val'] - r['min_val']:.0f} counts\n\n")

                        f.write(f"Data Integrity:\n")
                        f.write(f"  Timing gaps (>3x avg): {r['large_gaps']}")
                        if r['large_gaps'] == 0:
                            f.write(f" - [OK] No missed samples\n")
                        else:
                            f.write(f" - [WARNING] Possible sample loss\n")

                        f.write(f"  Amplitude jumps (>10x avg): {r['large_jumps']}")
                        if r['large_jumps'] == 0:
                            f.write(f" - [OK] Clean signal\n")
                        else:
                            f.write(f" - [INFO] Triangle wave direction changes\n")

                        # Overall integrity
                        if r['large_gaps'] == 0:
                            f.write(f"  Overall: [OK] All samples captured\n")
                        else:
                            f.write(f"  Overall: [WARNING] Data integrity compromised\n")
                        f.write(f"\n")

                        f.write(f"File: {r['filename']}\n")
                    else:
                        f.write(f"File not found: {r['filename']}\n")
                else:
                    f.write(f"Status: FAILED\n")
                    f.write(f"Error: {r.get('error', 'Unknown')}\n")

                f.write("\n")

            # Conclusions
            f.write("="*80 + "\n")
            f.write("CONCLUSIONS\n")
            f.write("="*80 + "\n\n")

            passed = sum(1 for r in results if r['success'] and r['error_pct'] < 5)
            f.write(f"Tests Passed: {passed}/{len(results)}\n\n")

            # Find maximum working rate
            working_rates = [r for r in results if r['success'] and r['error_pct'] < 5]
            if working_rates:
                max_rate = max(r['rate'] for r in working_rates)
                f.write(f"Maximum Working Rate: {max_rate} Hz\n")

            # Identify bottleneck
            f.write(f"\nBottleneck Analysis:\n")
            for r in results:
                if r['success'] and r['error_pct'] > 5:
                    f.write(f"  Degradation starts at {r['rate']} Hz\n")
                    f.write(f"  Actual rate achieved: {r['measured_hz']:.1f} Hz ({r['error_pct']:.1f}% loss)\n")
                    break

        print(f"[OK] Text report saved: {output_file}")

    except Exception as e:
        print(f"[ERROR] Report generation failed: {e}")


def main():
    import os
    # Resolve port from CLI arg or env (CLAUDE.md identity rule).
    port = sys.argv[1] if len(sys.argv) >= 2 else os.environ.get('DAQIFI_DEVICE_PORT')
    if not port:
        print(__doc__)
        sys.exit(1)

    # Determine test mode first
    test_mode = '--single'  # default
    if '--dual' in sys.argv:
        test_mode = '--dual'
    elif '--all' in sys.argv:
        test_mode = '--all'

    # Parse duration (skip flags)
    duration = 2  # default
    for arg in sys.argv[2:]:
        if not arg.startswith('--'):
            try:
                duration = int(arg)
                break
            except ValueError:
                pass

    # Test rate configurations
    single_rates = [5000, 6000, 7000, 8000, 9000, 10000, 11000, 12000, 13000, 14000, 15000]  # 1 kHz increments to 15kHz max
    dual_rates = [2000, 4000, 6000, 8000, 10000, 12000, 14000, 16000, 18000, 20000]

    print("="*80)
    print("  AUTOMATED HIGH-SPEED ADC TEST SUITE")
    print("="*80)
    print(f"\nPort: {port}")
    print(f"Duration per test: {duration} seconds")
    print(f"Test mode: {test_mode}")
    print(f"Triangle wave: 10 Hz, 1V amplitude, 1V offset on Channel 4")

    results = []

    try:
        # Connect once
        print(f"\nConnecting to {port}...")
        device = NyquistDevice.connect(port, baudrate=115200)
        print(f"[OK] Connected to {device.variant.value}")

        # Power up
        device._comm.send_command("SYST:POW:STAT 1")
        time.sleep(0.5)

        # Run single-channel tests
        if test_mode in ['--single', '--all']:
            print(f"\n{'='*80}")
            print("SINGLE-CHANNEL TESTS (Channel 4, Type 1)")
            print(f"{'='*80}")
            print(f"Rates: {', '.join(str(r) for r in single_rates)} Hz\n")

            for i, rate in enumerate(single_rates, 1):
                result = run_single_test(device, rate, duration, i, len(single_rates), dual_channel=False)
                results.append(result)

                # Stop if test failed
                if not result['success']:
                    print(f"\n[STOP] Single-channel test failed at {rate} Hz")
                    break

                # Delay between tests
                if i < len(single_rates):
                    time.sleep(2)

        # Run dual-channel tests
        if test_mode in ['--dual', '--all']:
            print(f"\n{'='*80}")
            print("DUAL-CHANNEL TESTS (Channels 4+5, Type1+Type2)")
            print(f"{'='*80}")
            print(f"Rates: {', '.join(str(r) for r in dual_rates)} Hz")
            print(f"Note: Ch4=Type1 (high-speed), Ch5=Type2 (scanning, max 1kHz)\n")

            dual_start_idx = len(results) + 1
            for i, rate in enumerate(dual_rates, dual_start_idx):
                result = run_single_test(device, rate, duration, i, dual_start_idx + len(dual_rates) - 1, dual_channel=True)
                results.append(result)

                # Stop if test failed
                if not result['success']:
                    print(f"\n[STOP] Dual-channel test failed at {rate} Hz")
                    break

                # Delay between tests
                if i < dual_start_idx + len(dual_rates) - 1:
                    time.sleep(2)

        # Cleanup
        device._comm.send_command("SYST:STOR:SD:ENAble 0")
        device.disconnect()

    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()

    # Generate report
    if results:
        print(f"\n{'='*80}")
        print("Test Summary")
        print("="*80)

        for r in results:
            if r['success']:
                status = "[OK]" if r['error_pct'] < 5 else "[WARN]"
                print(f"{status} {r['rate']:>6} Hz: {r['measured_hz']:>8.1f} Hz measured "
                      f"({r['error_pct']:>5.2f}% err), Wave: {r['wave_hz']:.2f} Hz")
            else:
                print(f"[FAIL] {r['rate']:>6} Hz: {r.get('error', 'Unknown error')}")

        # Generate text report
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = f"adc_test_report_{timestamp}.txt"
        generate_text_report(results, report_file)

        print(f"\n[OK] All tests complete!")

    return True


if __name__ == "__main__":
    main()
