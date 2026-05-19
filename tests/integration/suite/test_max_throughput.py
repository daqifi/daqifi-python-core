#!/usr/bin/env python3
"""Push every config to max with fast reader. Find true zero-leak ceilings.

Uses StreamingMeasurement from test_harness for accurate PC-controlled
rate measurement.
"""
import os
import sys, time
from pathlib import Path

from daqifi import NyquistDevice
from test_harness import ReliableSCPI, StreamingMeasurement


def test(scpi, ser, iface, fmt, channels, freq, dur=10):
    scpi.command('SYST:STReam:STOP', 1); scpi.drain(1)
    scpi.command('SYST:STR:BENCHmark 0'); scpi.command('SYST:STR:TESTpattern 0')
    scpi.command('SYST:STOR:SD:ENAble 0', 1); time.sleep(1)
    scpi.command('SYST:MEM:RESet', 0.3)
    scpi.command('ENABle:VOLTage:DC 0')
    for ch in channels: scpi.command(f'ENABle:VOLTage:DC {ch},1', 0.05)
    scpi.command(f'SYST:STR:FOR {fmt}')
    if iface == 2: scpi.command('SYST:STOR:SD:ENAble 1', 3); time.sleep(2)
    scpi.command(f'SYST:STR:INT {iface}')
    scpi.command('SYST:STR:TESTpattern 3')
    scpi.command('SYST:STR:BENCHmark 1')
    scpi.command('SYST:STR:ClearStats', 0.5); scpi.drain(0.5)

    m = StreamingMeasurement(scpi, ser)
    m.start(freq=freq, duration=dur, is_csv=(fmt == 2))

    scpi.command('SYST:STR:BENCHmark 0'); scpi.command('SYST:STR:TESTpattern 0')
    if iface == 2: scpi.command('SYST:STOR:SD:ENAble 0', 2); time.sleep(1)

    stats = m.fw_stats
    qd = stats.get('QueueDroppedSamples', 0)
    ud = stats.get('UsbDroppedBytes', 0)
    sd = stats.get('SdDroppedBytes', 0)
    return m.pc_sps, m.pc_kbps, m.leaked, qd, ud, sd


port = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DAQIFI_DEVICE_PORT')
device = NyquistDevice.connect(port)
scpi = ReliableSCPI(device)
ser = device._comm._serial

scpi.command('SYST:POW:STAT 1', 2); time.sleep(2)
ch1 = [0]; ch8 = list(range(8)); ch16 = list(range(16))

configs = [
    (0, 0, ch1,  [9000, 10000, 11000, 12000, 13000], "USB PB 1ch"),
    (0, 0, ch8,  [5000, 6000, 7000, 8000, 9000], "USB PB 8ch"),
    (0, 0, ch16, [3500, 4000, 4500, 5000, 5500, 6000], "USB PB 16ch"),
    (0, 2, ch1,  [11000, 13000, 15000, 17000], "USB CSV 1ch"),
    (0, 2, ch8,  [5000, 6000, 7000, 8000], "USB CSV 8ch"),
    (0, 2, ch16, [2000, 2500, 3000, 3500, 4000], "USB CSV 16ch"),
    (2, 0, ch1,  [9000, 10000, 11000, 12000, 13000], "SD PB 1ch"),
    (2, 0, ch8,  [5000, 6000, 7000, 8000, 9000], "SD PB 8ch"),
    (2, 0, ch16, [3000, 3500, 4000, 4500, 5000], "SD PB 16ch"),
    (2, 2, ch1,  [8000, 9000, 10000, 11000, 12000], "SD CSV 1ch"),
    (2, 2, ch8,  [2000, 2500, 3000, 3500], "SD CSV 8ch"),
    (2, 2, ch16, [1000, 1500, 2000, 2500], "SD CSV 16ch"),
]

print(f"{'Config':<20s}  {'Rate':>6s}  {'SPS':>7s}  {'KB/s':>7s}  Status")
print("-" * 65)

summary = []
for iface, fmt, chs, rates, label in configs:
    best_rate = 0; best_sps = 0; best_kbps = 0
    for rate in rates:
        sps, kbps, leak, qd, ud, sd = test(scpi, ser, iface, fmt, chs, rate)
        status = "LEAK" if leak else "CLEAN"
        drops = f"Q={qd} U={ud} S={sd}" if leak else ""
        print(f"{label:<20s}  {rate:>6d}  {sps:>7.0f}  {kbps:>6.0f}  {status} {drops}")
        if not leak:
            best_rate = rate; best_sps = round(sps); best_kbps = round(kbps)
        else:
            break
    summary.append((label, best_rate, best_sps, best_kbps))

print(f"\n{'='*65}")
print(f"{'Config':<20s}  {'Ceiling':>8s}  {'SPS':>7s}  {'KB/s':>7s}")
print(f"{'-'*65}")
for label, rate, sps, kbps in summary:
    print(f"{label:<20s}  {rate:>7d}   {sps:>7d}  {kbps:>6d}")

device.disconnect()
