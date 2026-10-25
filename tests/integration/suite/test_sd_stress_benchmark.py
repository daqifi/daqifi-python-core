#!/usr/bin/env python3
"""SD stress test — push beyond baseline limits with 32KB DMA write buffer."""

import os
import sys
import time
import csv
from pathlib import Path
from datetime import datetime

from daqifi import NyquistDevice
from test_harness import StreamingMeasurement


class ReliableSCPI:
    def __init__(self, device):
        self._device = device
        self._serial = device._comm._serial

    def drain(self, timeout=1.0):
        # Use monotonic for interval timing — wall-clock is subject to
        # NTP step / DST adjustments and isn't safe for deadlines.
        quiet_start = time.monotonic()
        total = 0
        while time.monotonic() - quiet_start < timeout:
            n = self._serial.in_waiting
            if n > 0:
                self._serial.read(n)
                total += n
                quiet_start = time.monotonic()
            else:
                time.sleep(0.05)
        return total

    def command(self, cmd, delay=0.3):
        self._serial.reset_input_buffer()
        self._serial.write(f"{cmd}\r".encode('ascii'))
        self._serial.flush()
        time.sleep(delay)

    def query_kv(self, cmd, delay=1.0):
        self.drain(0.3)
        self._serial.reset_input_buffer()
        self._serial.write(f"{cmd}\r".encode('ascii'))
        self._serial.flush()
        time.sleep(delay)
        chunks = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            n = self._serial.in_waiting
            if n > 0:
                chunks.append(self._serial.read(n))
                time.sleep(0.05)
            else:
                if chunks:
                    break
                time.sleep(0.1)
        raw = b''.join(chunks)
        text = raw.decode('ascii', errors='ignore')
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


def run_sd_test(scpi, fmt, channels, freq, duration_sec):
    fmt_names = {0: "PB", 2: "CSV"}
    label = f"SD {fmt_names[fmt]} {len(channels)}ch@{freq}Hz"
    print(f"  {label:<35s}", end='', flush=True)

    scpi.command("ENABle:VOLTage:DC 0")
    time.sleep(0.2)
    for ch in channels:
        scpi.command(f"ENABle:VOLTage:DC {ch},1", delay=0.05)

    scpi.command(f"SYST:STR:FOR {fmt}")
    scpi.command("SYST:STR:INT 2")  # SD
    scpi.command("SYST:STR:TESTpattern 2")
    scpi.command("SYST:STR:BENCHmark 1")
    scpi.command("SYST:STOR:SD:ENAble 1", delay=2.0)
    time.sleep(2)

    scpi.command("SYST:STR:ClearStats", delay=0.5)
    scpi.drain(0.5)

    scpi.command("SYST:MEM:AUTO", delay=0.5)

    m = StreamingMeasurement(scpi, scpi._serial)
    m.start(freq=freq, duration=duration_sec, is_csv=(fmt == 2),
            wait_for_serial=False)  # SD data goes to card, not serial

    scpi.command("SYST:STR:BENCHmark 0")
    scpi.command("SYST:STR:TESTpattern 0")

    stats = m.fw_stats
    total = stats.get('TotalSamplesStreamed', 0)
    total_bytes = stats.get('TotalBytesStreamed', 0)
    drops = stats.get('QueueDroppedSamples', 0)
    sd_drops = stats.get('SdDroppedBytes', 0)
    attempted = total + drops
    sps = m.pc_sps
    kbps = m.pc_kbps
    loss = (drops * 100 / attempted) if attempted > 0 else 0

    if total == 0 and attempted == 0:
        print(f"INVALID (no data)")
    elif loss == 0:
        print(f"PASS  {sps:>7.0f} sps  {kbps:>6.1f} KB/s  SdDrop={sd_drops}")
    else:
        print(f"LOSS {loss:>5.1f}%  {sps:>7.0f} sps  {kbps:>6.1f} KB/s  SdDrop={sd_drops}  QDrop={drops}")

    return {
        'format': fmt_names[fmt], 'channels': len(channels), 'freq': freq,
        'sps': round(sps), 'kbps': round(kbps, 1), 'loss': round(loss, 1),
        'sd_drops': sd_drops, 'queue_drops': drops, 'total': total,
    }


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DAQIFI_DEVICE_PORT')
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    print("=" * 70)
    print("  SD Throughput Stress Test (32KB DMA write buffer)")
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

    mem = scpi.query_kv("SYST:MEM:FREE?")
    print(f"  CoherentPool: {mem.get('CoherentPoolTotal', '?')} total, {mem.get('CoherentPoolFree', '?')} free")
    print(f"  SdCircular: {mem.get('SdCircularSize', '?')}")
    print(f"  SamplePool: {mem.get('SamplePoolCount', '?')}")

    all_ch = list(range(16))
    ch8 = list(range(8))

    # Baseline from daqifi-nyquist-firmware CLAUDE.md (8KB DMA buffer):
    # SD PB 1ch: 11kHz, SD PB 16ch: 7kHz
    # SD CSV 1ch: 5kHz, SD CSV 16ch: 1kHz
    # SD raw write: 665 KB/s

    print(f"\n  --- SD ProtoBuf (baseline: 1ch@11kHz, 16ch@7kHz) ---")
    tests_pb = [
        ([0],    5000),
        ([0],    7000),
        ([0],    11000),
        ([0],    13000),
        ([0],    15000),
        (ch8,    5000),
        (ch8,    7000),
        (ch8,    11000),
        (all_ch, 3000),
        (all_ch, 5000),
        (all_ch, 7000),
        (all_ch, 9000),
        (all_ch, 11000),
    ]

    results = []
    for channels, freq in tests_pb:
        try:
            r = run_sd_test(scpi, 0, channels, freq, duration)
            results.append(r)
        except Exception as e:
            print(f" ERROR: {e}")

    print(f"\n  --- SD CSV (baseline: 1ch@5kHz, 16ch@1kHz) ---")
    tests_csv = [
        ([0],    3000),
        ([0],    5000),
        ([0],    7000),
        ([0],    9000),
        ([0],    11000),
        (ch8,    1000),
        (ch8,    3000),
        (ch8,    5000),
        (all_ch, 1000),
        (all_ch, 2000),
        (all_ch, 3000),
    ]

    for channels, freq in tests_csv:
        try:
            r = run_sd_test(scpi, 2, channels, freq, duration)
            results.append(r)
        except Exception as e:
            print(f" ERROR: {e}")

    print(f"\n  --- Summary ---")
    print(f"  {'Config':<35s} {'SPS':>7s} {'KB/s':>7s} {'Loss':>6s}")
    print("  " + "-" * 58)
    for r in results:
        label = f"SD {r['format']} {r['channels']}ch@{r['freq']}Hz"
        loss = f"{r['loss']}%" if r['loss'] > 0 else "0%"
        print(f"  {label:<35s} {r['sps']:>7d} {r['kbps']:>6.1f} {loss:>6s}")

    # Save CSV
    csv_file = f"sd_stress_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    if results:
        with open(csv_file, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        print(f"\n  Saved to {csv_file}")

    device.disconnect()
    print("  Done!")


if __name__ == "__main__":
    main()
