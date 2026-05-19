#!/usr/bin/env python3
"""
Config sweep: every allowed interface × format × channel count at max throughput.
Verifies zero sample loss on each path. Rates set ~10% below measured ceilings
to account for run-to-run variance.

Allowed combos: USB, SD, WiFi (each solo). WiFi+SD blocked (shared SPI bus).

Usage:
    python3 test_config_sweep.py <port>
    python3 test_config_sweep.py <port> --wifi <device_ip>
    # Or rely on env vars: DAQIFI_DEVICE_PORT / DAQIFI_DEVICE_HOST
"""

import os
import sys
import time
import socket
import threading
import argparse
from pathlib import Path
from datetime import datetime

from daqifi import NyquistDevice
from test_harness import StreamingMeasurement, _assert_expected_device


class ReliableSCPI:
    def __init__(self, device):
        self._device = device
        self._serial = device._comm._serial

    def drain(self, timeout=1.0):
        quiet_start = time.time()
        total = 0
        while time.time() - quiet_start < timeout:
            n = self._serial.in_waiting
            if n > 0:
                self._serial.read(n)
                total += n
                quiet_start = time.time()
            else:
                time.sleep(0.05)
        return total

    def command(self, cmd, delay=0.3):
        self._serial.reset_input_buffer()
        self._serial.write(f"{cmd}\r".encode('ascii'))
        self._serial.flush()
        time.sleep(delay)

    def query(self, cmd, delay=1.0):
        self.drain(0.3)
        self._serial.reset_input_buffer()
        self._serial.write(f"{cmd}\r".encode('ascii'))
        self._serial.flush()
        time.sleep(delay)
        chunks = []
        deadline = time.time() + 2.0
        while time.time() < deadline:
            n = self._serial.in_waiting
            if n > 0:
                chunks.append(self._serial.read(n))
                time.sleep(0.05)
            else:
                if chunks:
                    break
                time.sleep(0.1)
        raw = b''.join(chunks)
        return raw.decode('ascii', errors='ignore')

    def query_kv(self, cmd, delay=1.0):
        text = self.query(cmd, delay)
        d = {}
        for line in text.split('\n'):
            if '=' in line:
                parts = line.split('=', 1)
                if len(parts) == 2:
                    try:
                        d[parts[0].strip()] = int(parts[1].strip())
                    except ValueError:
                        d[parts[0].strip()] = parts[1].strip()
        return d


class TcpDrainClient:
    """TCP client that connects and drains WiFi streaming data."""
    def __init__(self, host, port=9760):
        self.host = host
        self.port = port
        self.sock = None
        self._running = False
        self._thread = None
        self.bytes_received = 0

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(0.5)

    def start_drain(self):
        self._running = True
        self.bytes_received = 0
        self._thread = threading.Thread(target=self._drain_loop, daemon=True)
        self._thread.start()

    def _drain_loop(self):
        while self._running:
            try:
                data = self.sock.recv(65536)
                if data:
                    self.bytes_received += len(data)
                else:
                    break
            except socket.timeout:
                continue
            except Exception:
                break

    def stop_drain(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def disconnect(self):
        self.stop_drain()
        if self.sock:
            try:
                self.sock.close()
            except:
                pass


def device_reset(scpi):
    """Reset device to clean state between tests. Waits for SD idle.

    Identity-gated against DAQIFI_EXPECTED_SERIAL_NUMBER per CLAUDE.md.
    Local override of test_harness.device_reset because this script's
    SD interface needs an extra second after the disable for the file
    to close cleanly. (Qodo PR 143 pass 3 #6.)
    """
    _assert_expected_device(scpi)
    scpi.command("SYST:STReam:STOP", delay=1.0)
    scpi.drain(1.0)
    scpi.command("SYST:STR:BENCHmark 0", delay=0.2)
    scpi.command("SYST:STR:TESTpattern 0", delay=0.2)
    scpi.command("SYST:STOR:SD:ENAble 0", delay=2.0)
    # Wait for SD file close + buffer drain
    time.sleep(1)
    scpi.command("ENABle:VOLTage:DC 0", delay=0.2)
    scpi.command("SYST:STR:INT 0", delay=0.2)
    scpi.command("SYST:MEM:RESet", delay=0.3)
    scpi.drain(1.0)


def run_test(scpi, interface, fmt, channels, freq, duration, label, tcp=None):
    """Run a streaming session and verify zero sample loss."""
    fmt_names = {0: "PB", 2: "CSV"}
    iface_names = {0: "USB", 1: "WiFi", 2: "SD"}
    print(f"  {label:<45s}", end='', flush=True)

    # Clean state before every test
    device_reset(scpi)

    # Configure
    for ch in channels:
        scpi.command(f"ENABle:VOLTage:DC {ch},1", delay=0.05)

    scpi.command(f"SYST:STR:FOR {fmt}")
    scpi.command("SYST:STR:TESTpattern 2")

    if interface == 2:  # SD
        scpi.command("SYST:STOR:SD:ENAble 1", delay=3.0)
        time.sleep(2)

    scpi.command(f"SYST:STR:INT {interface}")
    scpi.command("SYST:STR:ClearStats", delay=0.5)
    scpi.drain(0.5)
    if tcp:
        tcp.bytes_received = 0

    # Stream — MemoryConfig is zeroed (from device_reset), STReam:START auto-balances
    m = StreamingMeasurement(scpi, scpi._serial)
    m.start(freq=freq, duration=duration, is_csv=(fmt == 2),
            wait_for_serial=(interface == 0))

    stats = m.fw_stats
    total = stats.get('TotalSamplesStreamed', 0)
    total_bytes = stats.get('TotalBytesStreamed', 0)
    drops = stats.get('QueueDroppedSamples', 0)
    usb_drops = stats.get('UsbDroppedBytes', 0)
    wifi_drops = stats.get('WifiDroppedBytes', 0)
    sd_drops = stats.get('SdDroppedBytes', 0)
    enc_fails = stats.get('EncoderFailures', 0)
    attempted = total + drops
    sps = m.pc_sps
    kbps = m.pc_kbps

    # Determine pass/fail
    if total == 0 and attempted == 0:
        print(f"INVALID (0 samples — stats likely stale)")
        return {'label': label, 'status': 'INVALID', 'sps': 0}

    sample_loss = drops > 0
    iface_drop = (interface == 0 and usb_drops > 0) or \
                 (interface == 1 and wifi_drops > 0) or \
                 (interface == 2 and sd_drops > 0)

    if sample_loss:
        loss_pct = drops * 100 / attempted
        print(f"FAIL  loss={loss_pct:.1f}%  sps={sps:.0f}  drops={drops}")
        return {'label': label, 'status': 'FAIL', 'sps': round(sps),
                'drops': drops, 'loss_pct': round(loss_pct, 1)}
    elif iface_drop:
        print(f"WARN  sps={sps:.0f}  {kbps:.1f} KB/s  byteDrop=USB:{usb_drops}/WiFi:{wifi_drops}/SD:{sd_drops}")
        return {'label': label, 'status': 'WARN', 'sps': round(sps),
                'usb_drops': usb_drops, 'wifi_drops': wifi_drops, 'sd_drops': sd_drops}
    else:
        print(f"PASS  sps={sps:.0f}  {kbps:.1f} KB/s")
        return {'label': label, 'status': 'PASS', 'sps': round(sps), 'kbps': round(kbps, 1)}


def main():
    # Resolve rig-specific identifiers from CLI → env (see CLAUDE.md).
    import os
    parser = argparse.ArgumentParser(description='Config sweep: max throughput on every allowed path')
    parser.add_argument('port', nargs='?', default=os.environ.get('DAQIFI_DEVICE_PORT'), help='USB serial port (or env DAQIFI_DEVICE_PORT)')
    parser.add_argument('--wifi', type=str, default=os.environ.get('DAQIFI_DEVICE_HOST'), help='Device WiFi IP (or env DAQIFI_DEVICE_HOST)')
    parser.add_argument('--duration', type=int, default=10, help='Seconds per test (default 10)')
    args = parser.parse_args()
    if not args.port:
        parser.error('port is required (CLI arg or DAQIFI_DEVICE_PORT env)')

    duration = args.duration
    print("=" * 70)
    print("  Max Throughput Config Sweep — Zero Loss Verification")
    print(f"  Port: {args.port}, WiFi: {args.wifi or 'disabled'}, Duration: {duration}s")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    device = NyquistDevice.connect(args.port)
    scpi = ReliableSCPI(device)

    try:
        device.power_up()
    except:
        scpi.command("SYST:POW:STAT 1", delay=3.0)
    time.sleep(2)

    idn = scpi.query("*IDN?", delay=0.5)
    mem = scpi.query_kv("SYST:MEM:FREE?", delay=1.0)
    print(f"  Device: {idn.strip()}")
    print(f"  Heap: {mem.get('HeapFree', '?')}, Pool: {mem.get('SamplePoolCount', '?')}")

    ch1 = [0]
    ch8 = list(range(8))
    ch16 = list(range(16))

    results = []

    # =========================================================================
    # USB tests — rates ~10% below measured ceilings for variance margin
    # Ceilings: PB 1ch@15k, PB 8ch@7k, PB 16ch@5k
    #           CSV 1ch@17k, CSV 8ch@5k, CSV 16ch@5k
    # =========================================================================
    print(f"\n  --- USB (max throughput, zero loss target) ---")
    usb_tests = [
        (0, 0, ch1,  13000, "USB PB  1ch@13kHz"),
        (0, 0, ch8,   6000, "USB PB  8ch@6kHz"),
        (0, 0, ch16,  4500, "USB PB 16ch@4.5kHz"),
        (0, 2, ch1,  11000, "USB CSV  1ch@11kHz"),
        (0, 2, ch8,   4000, "USB CSV  8ch@4kHz"),
        (0, 2, ch16,  1000, "USB CSV 16ch@1kHz"),  # 5kHz standalone; limited by shared USB CDC port (#248)
    ]
    for iface, fmt, chs, freq, label in usb_tests:
        r = run_test(scpi, iface, fmt, chs, freq, duration, label)
        results.append(r)

    # =========================================================================
    # SD tests — rates ~10% below measured ceilings
    # Ceilings: PB 1ch@13k, PB 8ch@7k, PB 16ch@7k
    #           CSV 1ch@11k, CSV 8ch@3k, CSV 16ch@2k
    # =========================================================================
    print(f"\n  --- SD (max throughput, zero loss target) ---")
    sd_tests = [
        (2, 0, ch1,  11000, "SD PB  1ch@11kHz"),
        (2, 0, ch8,   6000, "SD PB  8ch@6kHz"),
        (2, 0, ch16,  4500, "SD PB 16ch@4.5kHz"),
        (2, 2, ch1,   8000, "SD CSV  1ch@8kHz"),
        (2, 2, ch8,   2500, "SD CSV  8ch@2.5kHz"),
        (2, 2, ch16,  1800, "SD CSV 16ch@1.8kHz"),
    ]
    for iface, fmt, chs, freq, label in sd_tests:
        r = run_test(scpi, iface, fmt, chs, freq, duration, label)
        results.append(r)

    # =========================================================================
    # WiFi tests (optional — requires --wifi flag and TCP connection)
    # Ceilings: PB 1ch@5k, PB 8ch@3k, PB 16ch@3k
    #           CSV 1ch@5k, CSV 8ch@1k
    # =========================================================================
    tcp = None
    if args.wifi:
        print(f"\n  --- WiFi (max throughput, zero loss target) ---")
        tcp = TcpDrainClient(args.wifi)
        try:
            tcp.connect()
            tcp.start_drain()
            print(f"  TCP connected to {args.wifi}:9760")
        except Exception as e:
            print(f"  SKIP WiFi tests: TCP connect failed: {e}")
            tcp = None

    if tcp:
        wifi_tests = [
            (1, 0, ch1,   4500, "WiFi PB  1ch@4.5kHz"),
            (1, 0, ch8,   2500, "WiFi PB  8ch@2.5kHz"),
            (1, 0, ch16,  2500, "WiFi PB 16ch@2.5kHz"),
            (1, 2, ch1,   4500, "WiFi CSV  1ch@4.5kHz"),
            (1, 2, ch8,    900, "WiFi CSV  8ch@900Hz"),
        ]
        for iface, fmt, chs, freq, label in wifi_tests:
            r = run_test(scpi, iface, fmt, chs, freq, duration, label, tcp=tcp)
            results.append(r)
        tcp.disconnect()

    # =========================================================================
    # Rapid interface switching (functional, not throughput)
    # =========================================================================
    print(f"\n  --- Rapid interface switching (USB<->SD, 5 cycles) ---")
    print(f"  {'USB->SD->USB->SD->USB':<45s}", end='', flush=True)
    switch_ok = True
    for i in range(5):
        try:
            scpi.command("ENABle:VOLTage:DC 0")
            scpi.command("ENABle:VOLTage:DC 0,1", delay=0.05)
            scpi.command("SYST:STR:FOR 0")
            scpi.command("SYST:STR:TESTpattern 2")

            # USB
            scpi.command("SYST:STR:INT 0")
            scpi.command("SYST:MEM:RESet", delay=0.3)  # Zero MemoryConfig — let STReam:START auto-balance
            scpi.command("SYST:STReam:START 1000", delay=0.5)
            time.sleep(1)
            n = scpi._serial.in_waiting
            if n > 0: scpi._serial.read(n)
            scpi.command("SYST:STReam:STOP", delay=1.0)
            scpi.drain(0.5)

            # SD
            scpi.command("SYST:STOR:SD:ENAble 1", delay=1.0)
            scpi.command("SYST:STR:INT 2")
            scpi.command("SYST:MEM:RESet", delay=0.3)  # Zero MemoryConfig — let STReam:START auto-balance
            scpi.command("SYST:STReam:START 1000", delay=0.5)
            time.sleep(1)
            n = scpi._serial.in_waiting
            if n > 0: scpi._serial.read(n)
            scpi.command("SYST:STReam:STOP", delay=1.0)
            scpi.drain(0.5)
            scpi.command("SYST:STOR:SD:ENAble 0", delay=1.0)

            scpi.command("SYST:STR:TESTpattern 0")
        except Exception as e:
            print(f"FAIL cycle {i}: {e}")
            switch_ok = False
            break

    if switch_ok:
        idn = scpi.query("*IDN?", delay=0.5)
        if "DAQiFi" in idn:
            print(f"PASS")
        else:
            print(f"FAIL (device unresponsive)")
            switch_ok = False
    results.append({'label': 'Rapid switching (5 cycles)', 'status': 'PASS' if switch_ok else 'FAIL'})

    # =========================================================================
    # Health check
    # =========================================================================
    print(f"\n  --- Post-test health ---")
    mem = scpi.query_kv("SYST:MEM:FREE?", delay=1.0)
    print(f"  HeapMinEverFree: {mem.get('HeapMinEverFree', '?')}")
    print(f"  SamplePoolMaxUsed: {mem.get('SamplePoolMaxUsed', '?')}")

    # =========================================================================
    # Summary
    # =========================================================================
    print(f"\n  {'='*60}")
    passed = sum(1 for r in results if r.get('status') == 'PASS')
    warned = sum(1 for r in results if r.get('status') == 'WARN')
    failed = sum(1 for r in results if r.get('status') in ('FAIL', 'INVALID'))
    total_tests = len(results)

    for r in results:
        s = r.get('status', '?')
        marker = {'PASS': 'PASS', 'WARN': 'WARN', 'FAIL': 'FAIL', 'INVALID': 'FAIL'}.get(s, '????')
        extra = ''
        if s == 'FAIL' and 'loss_pct' in r:
            extra = f"  (loss={r['loss_pct']}%, drops={r['drops']})"
        elif s == 'WARN':
            extra = f"  (byte drops: USB={r.get('usb_drops',0)} WiFi={r.get('wifi_drops',0)} SD={r.get('sd_drops',0)})"
        print(f"  [{marker}] {r['label']}{extra}")

    print(f"\n  {passed} PASS, {warned} WARN, {failed} FAIL (of {total_tests} tests)")
    if failed == 0:
        print(f"  *** ALL TESTS PASSED (zero sample loss on every path) ***")
    else:
        print(f"  *** {failed} TESTS FAILED ***")

    device.disconnect()
    print("  Done!")


if __name__ == "__main__":
    main()
