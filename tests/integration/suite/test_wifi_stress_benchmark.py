#!/usr/bin/env python3
"""WiFi throughput stress test — streams over WiFi TCP, commands over USB."""

import sys
import time
import socket
import threading
import csv as csvmod
from pathlib import Path
from datetime import datetime

from daqifi import NyquistDevice
from test_harness import ReliableSCPI, StreamingMeasurement


class TcpDrainClient:
    """TCP client that connects and drains streaming data."""
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
        print(f"  TCP connected to {self.host}:{self.port}")

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


def run_wifi_test(scpi, tcp, fmt, channels, freq, duration_sec):
    fmt_names = {0: "PB", 2: "CSV"}
    label = f"WiFi {fmt_names[fmt]} {len(channels)}ch@{freq}Hz"
    print(f"  {label:<35s}", end='', flush=True)

    scpi.command("ENABle:VOLTage:DC 0")
    time.sleep(0.2)
    for ch in channels:
        scpi.command(f"ENABle:VOLTage:DC {ch},1", delay=0.05)

    scpi.command(f"SYST:STR:FOR {fmt}")
    scpi.command("SYST:STR:INT 1")  # WiFi
    scpi.command("SYST:STR:TESTpattern 2")
    scpi.command("SYST:STR:BENCHmark 1")

    scpi.command("SYST:STR:ClearStats", delay=0.5)
    scpi.drain(0.5)
    tcp.bytes_received = 0

    scpi.command("SYST:MEM:AUTO", delay=0.5)

    # Use StreamingMeasurement for PC-controlled timing window.
    # WiFi data arrives via TCP, not serial — skip first-byte wait.
    m = StreamingMeasurement(scpi, scpi._serial)
    m.start(freq=freq, duration=duration_sec, is_csv=(fmt == 2),
            wait_for_serial=False)

    scpi.command("SYST:STR:BENCHmark 0")
    scpi.command("SYST:STR:TESTpattern 0")

    stats = m.fw_stats
    total = stats.get('TotalSamplesStreamed', 0)
    total_bytes = stats.get('TotalBytesStreamed', 0)
    drops = stats.get('QueueDroppedSamples', 0)
    wifi_drops = stats.get('WifiDroppedBytes', 0)
    attempted = total + drops
    # WiFi: data arrives via TCP, not serial — use TCP bytes for KB/s,
    # firmware total for SPS (serial-based m.pc_sps/m.pc_kbps will be 0)
    sps = total / m.window_duration if total > 0 and m.window_duration > 0 else 0
    tcp_kbps = tcp.bytes_received / m.window_duration / 1024 if m.window_duration > 0 else 0
    kbps = tcp_kbps
    loss = (drops * 100 / attempted) if attempted > 0 else 0

    if total == 0 and attempted == 0:
        print(f"INVALID (no data)")
    elif loss == 0 and wifi_drops == 0:
        print(f"PASS  {sps:>7.0f} sps  {kbps:>6.1f} KB/s  tcp={tcp_kbps:.1f} KB/s")
    elif loss == 0:
        print(f"PASS  {sps:>7.0f} sps  {kbps:>6.1f} KB/s  WifiDrop={wifi_drops}  tcp={tcp_kbps:.1f} KB/s")
    else:
        print(f"LOSS {loss:>5.1f}%  {sps:>7.0f} sps  {kbps:>6.1f} KB/s  WifiDrop={wifi_drops}  QDrop={drops}")

    return {
        'format': fmt_names[fmt], 'channels': len(channels), 'freq': freq,
        'sps': round(sps), 'kbps': round(kbps, 1), 'loss': round(loss, 1),
        'wifi_drops': wifi_drops, 'queue_drops': drops, 'total': total,
        'tcp_kbps': round(tcp_kbps, 1),
    }


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyACM0'
    host = sys.argv[2] if len(sys.argv) > 2 else '192.168.1.160'
    duration = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    print("=" * 70)
    print("  WiFi Throughput Stress Test (8KB SPI staging buffer)")
    print(f"  USB: {port}, WiFi: {host}:9760, Duration: {duration}s per test")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    device = NyquistDevice.connect(port)
    scpi = ReliableSCPI(device)

    try:
        device.power_up()
    except:
        scpi.command("SYST:POW:STAT 1", delay=3.0)
    time.sleep(2)

    # Connect TCP client
    tcp = TcpDrainClient(host)
    try:
        tcp.connect()
    except Exception as e:
        print(f"  FAILED to connect TCP: {e}")
        device.disconnect()
        return
    tcp.start_drain()

    all_ch = list(range(16))
    ch8 = list(range(8))

    print(f"\n  --- WiFi PB ---")
    tests_pb = [
        ([0],    500),
        ([0],    1000),
        ([0],    3000),
        ([0],    5000),
        (ch8,    500),
        (ch8,    1000),
        (ch8,    3000),
        (all_ch, 500),
        (all_ch, 1000),
        (all_ch, 2000),
        (all_ch, 3000),
    ]

    results = []
    for channels, freq in tests_pb:
        try:
            r = run_wifi_test(scpi, tcp, 0, channels, freq, duration)
            results.append(r)
        except Exception as e:
            print(f" ERROR: {e}")

    print(f"\n  --- WiFi CSV ---")
    tests_csv = [
        ([0],    500),
        ([0],    1000),
        ([0],    3000),
        ([0],    5000),
        (ch8,    500),
        (ch8,    1000),
        (all_ch, 500),
        (all_ch, 1000),
        (all_ch, 2000),
    ]

    for channels, freq in tests_csv:
        try:
            r = run_wifi_test(scpi, tcp, 2, channels, freq, duration)
            results.append(r)
        except Exception as e:
            print(f" ERROR: {e}")

    tcp.disconnect()

    print(f"\n  --- Summary ---")
    print(f"  {'Config':<35s} {'SPS':>7s} {'KB/s':>7s} {'Loss':>6s} {'TCP':>7s}")
    print("  " + "-" * 65)
    for r in results:
        label = f"WiFi {r['format']} {r['channels']}ch@{r['freq']}Hz"
        loss = f"{r['loss']}%" if r['loss'] > 0 else "0%"
        print(f"  {label:<35s} {r['sps']:>7d} {r['kbps']:>6.1f} {loss:>6s} {r['tcp_kbps']:>6.1f}")

    csv_file = f"wifi_stress_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    if results:
        with open(csv_file, 'w', newline='') as f:
            writer = csvmod.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        print(f"\n  Saved to {csv_file}")

    device.disconnect()
    print("  Done!")


if __name__ == "__main__":
    main()
