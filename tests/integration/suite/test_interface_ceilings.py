#!/usr/bin/env python3
"""
Find zero-leak ceilings for all interface×format×channel combos.
Runs FOUR tiers:
  1. Normal (real data, freq cap)
  2. TestPattern (fullscale, freq cap)
  3. NoCap (fullscale, cap bypassed)
  4. Pipeline (skip ADC, cap bypassed)

Uses FastReader (1ms thread). Saves results to benchmarks/ as CSV.

Usage:
    python3 test_interface_ceilings.py /dev/ttyACM0
    python3 test_interface_ceilings.py /dev/ttyACM0 10 --wifi 192.168.1.160
"""

import sys, time, threading, csv as csvmod, os, socket, argparse
from pathlib import Path
from datetime import datetime

from daqifi import NyquistDevice


class ReliableSCPI:
    def __init__(self, device):
        self._serial = device._comm._serial

    def drain(self, t=1.0):
        """Drain serial port until it stays quiet for `t` seconds."""
        q = time.time()
        while time.time() - q < t:
            try:
                n = self._serial.in_waiting
            except Exception:
                time.sleep(0.05); continue
            if n > 0:
                self._serial.read(n); q = time.time()
            else:
                time.sleep(0.05)

    def command(self, c, delay=0.3):
        try:
            self._serial.reset_input_buffer()
        except Exception:
            pass
        self._serial.write(f'{c}\r'.encode()); self._serial.flush(); time.sleep(delay)

    def query_kv(self, c, delay=1.5, expected_keys=None, max_retries=3):
        """Send a query and parse key=value response.

        Aggressively drains the serial port BEFORE the query to clear any
        residual streaming bytes (otherwise binary data contaminates the
        parsed response and missing fields silently default to 0).

        If `expected_keys` is provided, validates that ALL listed keys are
        present in the parsed response. Retries up to `max_retries` times,
        each retry with a longer drain. Raises RuntimeError on persistent
        failure so the caller knows the data is bad rather than silently
        getting zeros for missing fields.
        """
        for attempt in range(max_retries):
            # Drain longer on each retry to give the firmware time to flush
            # any in-flight USB DMA / circular buffer contents.
            drain_time = 0.5 + 0.5 * attempt
            self.drain(drain_time)

            try:
                self._serial.reset_input_buffer()
            except Exception:
                pass
            self._serial.write(f'{c}\r'.encode())
            self._serial.flush()
            time.sleep(delay)

            chunks = []
            dl = time.time() + 2
            while time.time() < dl:
                try:
                    n = self._serial.in_waiting
                except Exception:
                    break
                if n > 0:
                    chunks.append(self._serial.read(n)); time.sleep(0.05)
                elif chunks:
                    break
                else:
                    time.sleep(0.1)

            d = {}
            for line in b''.join(chunks).decode('ascii', errors='ignore').split('\n'):
                if '=' in line:
                    p = line.split('=', 1)
                    if len(p) == 2:
                        key = p[0].strip()
                        try:
                            d[key] = int(p[1].strip())
                        except ValueError:
                            d[key] = p[1].strip()

            if expected_keys is None:
                return d
            missing = [k for k in expected_keys if k not in d]
            if not missing:
                return d
            # Otherwise retry — likely contaminated by leftover stream bytes.

        raise RuntimeError(
            f"query '{c}' missing expected keys after {max_retries} retries: {missing}")


class TcpDrainClient:
    """TCP client that drains WiFi streaming data in background."""
    def __init__(self, host, port=9760):
        self.host = host; self.port = port
        self.sock = None; self._running = False; self._thread = None
        self.bytes_received = 0
    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5); self.sock.connect((self.host, self.port))
        self.sock.settimeout(0.5)
    def start(self):
        self._running = True; self.bytes_received = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
    def _loop(self):
        while self._running:
            try:
                data = self.sock.recv(65536)
                if data: self.bytes_received += len(data)
                else: break
            except socket.timeout: continue
            except: break
    def stop(self):
        self._running = False
        if self._thread: self._thread.join(timeout=3)
    def close(self):
        self.stop()
        try: self.sock.close()
        except: pass


def test(scpi, ser, iface, fmt, channels, freq, benchmark_mode, test_pattern=3, tcp=None, dur=10):
    """Run a single streaming test using PC-controlled measurement window.

    Uses StreamingMeasurement from test_harness for accurate rate calculation.
    PC controls the timing window — no firmware trust for rate numbers.
    """
    from test_harness import StreamingMeasurement

    # Setup: stop, reset, configure
    scpi.command('SYST:StopStreamData', 1); scpi.drain(1)
    scpi.command('SYST:STR:BENCHmark 0'); scpi.command('SYST:STR:TESTpattern 0')
    scpi.command('SYST:STOR:SD:ENAble 0', 1); time.sleep(1)
    scpi.command('SYST:MEM:RESet', 0.3)
    scpi.command('ENABle:VOLTage:DC 0')
    for ch in channels: scpi.command(f'ENABle:VOLTage:DC {ch},1', 0.05)
    scpi.command(f'SYST:STR:FOR {fmt}')
    if iface == 2: scpi.command('SYST:STOR:SD:ENAble 1', 3); time.sleep(2)
    scpi.command(f'SYST:STR:INT {iface}')
    scpi.command(f'SYST:STR:TESTpattern {test_pattern}')
    scpi.command(f'SYST:STR:BENCHmark {benchmark_mode}')
    scpi.command('SYST:STR:ClearStats', 0.5); scpi.drain(0.5)
    if tcp: tcp.bytes_received = 0; tcp.start()

    # Measure using PC-controlled window (excludes SCPI command latency)
    m = StreamingMeasurement(scpi, ser)
    m.start(freq=freq, duration=dur, is_csv=(fmt == 2),
            wait_for_serial=(iface == 0))

    if tcp: tcp.stop()

    # Cleanup
    scpi.command('SYST:STR:BENCHmark 0'); scpi.command('SYST:STR:TESTpattern 0')
    if iface == 2: scpi.command('SYST:STOR:SD:ENAble 0', 2); time.sleep(1)

    stats = m.fw_stats
    isr_calls = stats.get('TimerISRCalls', 0)
    qd = stats.get('QueueDroppedSamples', 0)
    total = stats.get('TotalSamplesStreamed', 0)

    # Timer health check
    if isr_calls > 0 and m.window_duration > 0:
        isr_hz = isr_calls / m.window_duration
        isr_ratio = isr_hz / freq
        if isr_ratio < 0.98:
            print(f"    [TIMER THROTTLED] cmd={freq} isr_hz={isr_hz:.0f} "
                  f"ratio={isr_ratio:.3f} samples={total} drops={qd}")

    return m.pc_sps, m.pc_kbps, m.leaked


def find_ceiling(scpi, ser, iface, fmt, channels, rates, label, benchmark_mode, test_pattern=3, tcp=None, dur=10):
    best_rate = 0; best_sps = 0; best_kbps = 0
    for rate in rates:
        sps, kbps, leak = test(scpi, ser, iface, fmt, channels, rate, benchmark_mode, test_pattern, tcp, dur)
        status = "LEAK" if leak else "CLEAN"
        print(f"    {rate:>6d}  {sps:>7.0f}  {kbps:>6.0f}  {status}")
        if not leak:
            best_rate = rate; best_sps = round(sps); best_kbps = round(kbps)
        else:
            break
    return best_rate, best_sps, best_kbps


def main():
    parser = argparse.ArgumentParser(description='4-tier ceiling test for all interfaces')
    parser.add_argument('port', nargs='?', default='/dev/ttyACM0')
    parser.add_argument('duration', nargs='?', type=int, default=10)
    parser.add_argument('--wifi', type=str, default=None, help='Device WiFi IP for WiFi tests')
    args = parser.parse_args()
    dur = args.duration

    device = NyquistDevice.connect(args.port)
    scpi = ReliableSCPI(device)
    ser = device._comm._serial

    scpi.command('SYST:POW:STAT 1', 2)
    time.sleep(2)

    # Connect WiFi TCP drain client if --wifi provided
    tcp = None
    if args.wifi:
        tcp = TcpDrainClient(args.wifi)
        try:
            tcp.connect()
            print(f"  TCP connected to {args.wifi}:9760")
        except Exception as e:
            print(f"  WiFi TCP connect failed: {e} — skipping WiFi tests")
            tcp = None

    ch1 = [0]; ch8 = list(range(8)); ch16 = list(range(16))

    # Rates go high enough for pipeline mode (skip ADC, much faster).
    # Ceiling finder stops at first leak, so extra rates only cost time
    # for the fastest tier.
    configs = [
        (0, 0, ch1,  [9000, 11000, 13000, 15000, 20000, 30000], "USB PB 1ch"),
        (0, 0, ch8,  [5000, 7000, 9000, 11000, 15000, 20000], "USB PB 8ch"),
        (0, 0, ch16, [3500, 4500, 5500, 6500, 8000, 11000], "USB PB 16ch"),
        (0, 2, ch1,  [11000, 13000, 15000, 17000, 20000, 30000], "USB CSV 1ch"),
        (0, 2, ch8,  [5000, 7000, 8000, 9000, 11000, 15000], "USB CSV 8ch"),
        (0, 2, ch16, [2000, 3000, 4000, 5000, 7000, 9000], "USB CSV 16ch"),
        (2, 0, ch1,  [9000, 11000, 13000, 15000, 20000, 30000], "SD PB 1ch"),
        (2, 0, ch8,  [5000, 7000, 9000, 11000, 15000], "SD PB 8ch"),
        (2, 0, ch16, [3000, 4000, 5000, 7000, 9000], "SD PB 16ch"),
        (2, 2, ch1,  [8000, 10000, 11000, 13000, 15000], "SD CSV 1ch"),
        (2, 2, ch8,  [2500, 3000, 3500, 4000, 5000], "SD CSV 8ch"),
        (2, 2, ch16, [1000, 1500, 2000, 3000, 4000], "SD CSV 16ch"),
    ]

    # WiFi configs (only if TCP client connected)
    if tcp:
        configs.extend([
            (1, 0, ch1,  [1000, 3000, 5000, 7000, 9000], "WiFi PB 1ch"),
            (1, 0, ch8,  [1000, 2000, 3000, 5000, 7000], "WiFi PB 8ch"),
            (1, 0, ch16, [1000, 2000, 3000, 4000, 5000], "WiFi PB 16ch"),
            (1, 2, ch1,  [1000, 3000, 5000, 7000], "WiFi CSV 1ch"),
            (1, 2, ch8,  [500, 1000, 2000, 3000], "WiFi CSV 8ch"),
            (1, 2, ch16, [500, 1000, 1500, 2000], "WiFi CSV 16ch"),
        ])

    all_results = []

    tiers = [
        ("NORMAL (real data, freq cap)",      0, 0),  # benchmark=0, pattern=0
        ("TESTPATTERN (fullscale, freq cap)", 0, 3),  # benchmark=0, pattern=3
        ("NOCAP (fullscale, cap bypassed)",   1, 3),  # benchmark=1, pattern=3
        ("PIPELINE (skip ADC, cap bypassed)", 2, 3),  # benchmark=2, pattern=3
    ]

    for mode_name, benchmark_mode, test_pattern in tiers:
        print(f"\n{'='*70}")
        print(f"  {mode_name} — FastReader")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}, {dur}s per test")
        print(f"{'='*70}")

        results = []
        for iface, fmt, chs, rates, label in configs:
            print(f"\n  {label}:")
            print(f"    {'Rate':>6s}  {'SPS':>7s}  {'KB/s':>6s}  Status")
            tcp_for_test = tcp if iface == 1 else None
            ceiling, sps, kbps = find_ceiling(scpi, ser, iface, fmt, chs, rates, label, benchmark_mode, test_pattern, tcp_for_test, dur)
            results.append({
                'mode': mode_name.split('(')[0].strip().lower(),
                'config': label,
                'ceiling_hz': ceiling,
                'sps': sps,
                'kbps': kbps,
            })
            print(f"    >>> Ceiling: {ceiling} Hz, {sps} sps, {kbps} KB/s")

        all_results.extend(results)

        print(f"\n  {'Config':<20s}  {'Ceiling':>8s}  {'SPS':>7s}  {'KB/s':>7s}")
        print(f"  {'-'*48}")
        for r in results:
            print(f"  {r['config']:<20s}  {r['ceiling_hz']:>7d}   {r['sps']:>7d}  {r['kbps']:>6d}")

    # Save CSV
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    csv_file = f"benchmarks/ceilings_{ts}.csv"
    os.makedirs("benchmarks", exist_ok=True)
    with open(csv_file, 'w', newline='') as f:
        writer = csvmod.DictWriter(f, fieldnames=['mode', 'config', 'ceiling_hz', 'sps', 'kbps'])
        writer.writeheader()
        writer.writerows(all_results)
    print(f"\n  Saved to {csv_file}")

    if tcp: tcp.close()
    device.disconnect()
    print("  Done!")


if __name__ == "__main__":
    main()
