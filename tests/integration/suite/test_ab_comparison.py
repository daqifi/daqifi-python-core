#!/usr/bin/env python3
"""A/B branch comparison: fast reader, push to max, check for any leaks."""
import sys, time, threading
from daqifi import NyquistDevice
from test_harness import ReliableSCPI, StreamingMeasurement

device = NyquistDevice.connect('/dev/ttyACM0')
scpi = ReliableSCPI(device)
ser = device._comm._serial

def run(interface, fmt, channels, freq, label, duration=10):
    fmt_names = {0: "PB", 2: "CSV"}
    print(f"  {label:<40s}", end='', flush=True)

    scpi.command('SYST:StopStreamData', 1); scpi.drain(1)
    scpi.command('SYST:STR:BENCHmark 0'); scpi.command('SYST:STR:TESTpattern 0')
    scpi.command('SYST:STOR:SD:ENAble 0', 1)
    scpi.command('SYST:MEM:RESet', 0.3)
    scpi.command('ENABle:VOLTage:DC 0')
    for ch in channels: scpi.command(f'ENABle:VOLTage:DC {ch},1', 0.05)
    scpi.command(f'SYST:STR:FOR {fmt}')
    if interface == 2:
        scpi.command('SYST:STOR:SD:ENAble 1', 3); time.sleep(2)
    scpi.command(f'SYST:STR:INT {interface}')
    scpi.command('SYST:STR:TESTpattern 2')
    scpi.command('SYST:STR:ClearStats', 0.5); scpi.drain(0.5)

    m = StreamingMeasurement(scpi, ser)
    m.start(freq=freq, duration=duration, is_csv=(fmt == 2))

    scpi.command('SYST:STR:TESTpattern 0')
    if interface == 2: scpi.command('SYST:STOR:SD:ENAble 0', 2); time.sleep(1)

    stats = m.fw_stats
    q_drops = stats.get('QueueDroppedSamples', 0)
    usb_drops = stats.get('UsbDroppedBytes', 0)
    sd_drops = stats.get('SdDroppedBytes', 0)
    wifi_drops = stats.get('WifiDroppedBytes', 0)
    any_loss = q_drops > 0 or usb_drops > 0 or sd_drops > 0 or wifi_drops > 0
    sps = m.pc_sps
    kbps = m.pc_kbps  # PC-measured bytes in window / window duration

    status = "LEAK" if any_loss else "CLEAN"
    drops = f"Q={q_drops} U={usb_drops} S={sd_drops}" if any_loss else ""
    print(f"{status}  {sps:>7.0f} sps  {kbps:>6.0f} KB/s  {drops}")
    return {'label': label, 'sps': round(sps), 'kbps': round(kbps), 'status': status,
            'q_drops': q_drops, 'usb_drops': usb_drops, 'sd_drops': sd_drops}

# Setup
scpi.command('SYST:POW:STAT 1', 2)
mem = scpi.query_kv('SYST:MEM:FREE?')
print(f"Pool={mem.get('SamplePoolCount','?')} Heap={mem.get('HeapFree','?')} Coherent={mem.get('CoherentPoolTotal','?')}")
print()

ch1 = [0]; ch8 = list(range(8)); ch16 = list(range(16))

tests = [
    (0, 0, ch1,  11000, "USB PB  1ch@11kHz"),
    (0, 0, ch8,   5000, "USB PB  8ch@5kHz"),
    (0, 0, ch16,  5000, "USB PB 16ch@5kHz"),
    (0, 2, ch1,  11000, "USB CSV  1ch@11kHz"),
    (0, 2, ch1,  15000, "USB CSV  1ch@15kHz"),
    (0, 2, ch8,   5000, "USB CSV  8ch@5kHz"),
    (0, 2, ch16,  1000, "USB CSV 16ch@1kHz"),
    (0, 2, ch16,  3000, "USB CSV 16ch@3kHz"),
    (0, 2, ch16,  5000, "USB CSV 16ch@5kHz"),
    (2, 0, ch1,  11000, "SD PB  1ch@11kHz"),
    (2, 0, ch16,  5000, "SD PB 16ch@5kHz"),
    (2, 2, ch1,   5000, "SD CSV  1ch@5kHz"),
    (2, 2, ch8,   3000, "SD CSV  8ch@3kHz"),
    (2, 2, ch16,  1000, "SD CSV 16ch@1kHz"),
    (2, 2, ch16,  2000, "SD CSV 16ch@2kHz"),
]

results = []
for iface, fmt, chs, freq, label in tests:
    r = run(iface, fmt, chs, freq, label)
    results.append(r)

print()
leaks = [r for r in results if r['status'] == 'LEAK']
clean = [r for r in results if r['status'] == 'CLEAN']
print(f"{len(clean)} CLEAN, {len(leaks)} LEAK out of {len(results)} tests")
if leaks:
    print("LEAKS:")
    for r in leaks:
        print(f"  {r['label']}: Q={r['q_drops']} U={r['usb_drops']} S={r['sd_drops']}")
else:
    print("*** ZERO LEAKS — FIRMWARE IS LEAK-FREE ***")

device.disconnect()
