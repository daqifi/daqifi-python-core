#!/usr/bin/env python3
"""
Comprehensive overnight streaming characterization test.

Finds zero-loss ceilings for every combination of:
  - Interface:  USB, SD
  - Format:     ProtoBuf (0), CSV (2)
  - Channels:   1×T1, 3×T1, 5×T1, 1×T2, 4×T2, 5×T2, 8×T2, 11×T2,
                5T1+4T2 (9ch), 5T1+11T2 (16ch)
  - Mode:       NoCap (real ADC, bypass freq cap)

Then runs endurance tests at each ceiling for extended duration.

Total: ~40 configs × ~8 rate steps × 10s = ~53 min ceiling sweep
       + 40 configs × 60s endurance = ~40 min endurance
       ≈ 1.5-2 hours total

Usage:
    python3 test_overnight_characterization.py [port] [--duration N] [--endurance N]
    python3 test_overnight_characterization.py /dev/ttyACM0 --duration 10 --endurance 60

Requires: daqifi-python-core (pip install -e ../daqifi-python-core)
"""

import sys, os, time, threading, csv as csvmod, argparse
from pathlib import Path
from datetime import datetime

from daqifi import NyquistDevice

# ─── NQ1 Channel Map ─────────────────────────────────────────────
# Board config index → (DaqifiAdcChannelId, Type, ChannelType)
# T1 dedicated: indices 3,7,9,11,13 → ch4,ch8,ch10,ch12,ch14
# T2 shared:    indices 0,1,2,4,5,6,8,10,12,14,15 → ch0-3,5-7,9,11,13,15
#
# For SCPI: ENABle:VOLTage:DC <daqifiChannelId>,1
# Public channels 0-15 map 1:1 to DaqifiAdcChannelId on NQ1.

# T1 channels (dedicated SAR modules, simultaneous conversion)
T1_CHANNELS = [4, 8, 10, 12, 14]  # NQ1 Type 1 DaqifiAdcChannelIds

# T2 channels (shared MODULE7, sequential mux scan)
T2_CHANNELS = [0, 1, 2, 3, 5, 6, 7, 9, 11, 13, 15]  # NQ1 Type 2

ALL_CHANNELS = list(range(16))

# ─── Channel configurations ──────────────────────────────────────
# Mirrored counts (1/3/5) for direct T1 vs T2 comparison,
# then scaling tests, then mixed configs.
# Tuple: (label, channels, obdiag_override)
#   obdiag_override: None = use default (ON), 0 = force OFF, 1 = force ON
CHANNEL_CONFIGS = [
    # --- Direct T1 vs T2 comparison (matched counts) ---
    ("1×T1",            T1_CHANNELS[:1],    None),   # ch4
    ("1×T2",            T2_CHANNELS[:1],    None),   # ch0
    ("3×T1",            T1_CHANNELS[:3],    None),   # ch4,8,10
    ("3×T2",            T2_CHANNELS[:3],    None),   # ch0,1,2
    ("5×T1",            T1_CHANNELS[:5],    None),   # all T1
    ("5×T2",            T2_CHANNELS[:5],    None),   # ch0,1,2,3,5
    # --- T1 with/without monitoring overhead ---
    ("5×T1 OBD=OFF",    T1_CHANNELS[:5],    0),      # T1 without monitoring reads
    ("5×T1 OBD=ON",     T1_CHANNELS[:5],    1),      # T1 with monitoring reads (default)
    ("1×T1 OBD=OFF",    T1_CHANNELS[:1],    0),      # single T1 no monitoring
    ("1×T1 OBD=ON",     T1_CHANNELS[:1],    1),      # single T1 with monitoring
    # --- T2 scaling ---
    ("8×T2",            T2_CHANNELS[:8],    None),   # ch0,1,2,3,5,6,7,9
    ("11×T2",           T2_CHANNELS[:11],   None),   # all T2
    # --- Mixed ---
    ("5T1+3T2",         T1_CHANNELS + T2_CHANNELS[:3],  None),  # 8ch mixed
    ("5T1+5T2",         T1_CHANNELS + T2_CHANNELS[:5],  None),  # 10ch mixed
    ("5T1+11T2",        ALL_CHANNELS,       None),   # 16ch all
]

# ─── Rate sweep steps (Hz) ───────────────────────────────────────
# Coarse enough to finish in reasonable time, fine enough to find ceilings.
# Ceiling finder stops at first leak, so unused upper rates are free.
RATE_STEPS = [
    1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000,
    11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000, 19000, 20000,
    22000, 25000, 30000,
]


class ReliableSCPI:
    def __init__(self, device):
        self._serial = device._comm._serial

    def drain(self, t=1.0):
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
        for attempt in range(max_retries):
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
                        try:
                            d[p[0].strip()] = int(p[1].strip())
                        except ValueError:
                            d[p[0].strip()] = p[1].strip()
            if expected_keys is None:
                return d
            if not [k for k in expected_keys if k not in d]:
                return d
        raise RuntimeError(f"query '{c}' missing keys after {max_retries} retries")


def run_test(scpi, ser, iface, fmt, channels, freq, dur=10,
             benchmark=1, pattern=3, obdiag=None):
    """Run a single streaming test using StreamingMeasurement.

    Returns (sps, kbps, stats_dict, leaked).
    obdiag: None = leave at current setting, 0 = force OFF, 1 = force ON
    """
    from test_harness import StreamingMeasurement

    # Reset state
    scpi.command('SYST:StopStreamData', 1); scpi.drain(1)
    scpi.command('SYST:STR:BENCHmark 0')
    scpi.command('SYST:STR:TESTpattern 0')
    scpi.command('SYST:STOR:SD:ENAble 0', 1); time.sleep(0.5)

    # Configure channels
    scpi.command('ENABle:VOLTage:DC 0')
    for ch in channels:
        scpi.command(f'ENABle:VOLTage:DC {ch},1', 0.05)

    # Configure onboard diagnostics if specified
    if obdiag is not None:
        scpi.command(f'CONF:ADC:OBDiag {obdiag}')

    # Configure interface and format
    scpi.command(f'SYST:STR:FOR {fmt}')
    if iface == 2:
        scpi.command('SYST:STOR:SD:ENAble 1', 3)
        time.sleep(2)
    scpi.command(f'SYST:STR:INT {iface}')
    scpi.command(f'SYST:STR:TESTpattern {pattern}')
    scpi.command(f'SYST:STR:BENCHmark {benchmark}')
    scpi.command('SYST:STR:ClearStats', 0.5)
    scpi.drain(0.5)

    # PC-controlled measurement window
    m = StreamingMeasurement(scpi, ser)
    m.start(freq=freq, duration=dur, is_csv=(fmt == 2),
            wait_for_serial=(iface == 0))

    # Cleanup
    scpi.command('SYST:STR:BENCHmark 0')
    scpi.command('SYST:STR:TESTpattern 0')
    if obdiag is not None:
        scpi.command('CONF:ADC:OBDiag 1')
    if iface == 2:
        scpi.command('SYST:STOR:SD:ENAble 0', 2); time.sleep(1)

    return m.pc_sps, m.pc_kbps, m.fw_stats, m.leaked


def find_ceiling(scpi, ser, iface, fmt, channels, label, dur=10, obdiag=None):
    """Binary-ish search for max zero-loss rate."""
    best_rate = 0; best_sps = 0; best_kbps = 0; best_stats = {}

    print(f"    {'Rate':>6s}  {'SPS':>7s}  {'KB/s':>6s}  {'Drops':>8s}  {'EOS':>5s}  Status")
    for rate in RATE_STEPS:
        sps, kbps, stats, leaked = run_test(scpi, ser, iface, fmt, channels, rate, dur, obdiag=obdiag)
        qd = stats.get('QueueDroppedSamples', 0)
        eos = stats.get('EosOverruns', 0)
        dio = stats.get('DioDroppedSamples', 0)
        enc = stats.get('EncoderDroppedSamples', 0)
        status = "LEAK" if leaked else "CLEAN"
        print(f"    {rate:>6d}  {sps:>7.0f}  {kbps:>6.0f}  {qd:>8d}  {eos:>5d}  {status}")
        if not leaked:
            best_rate = rate; best_sps = round(sps); best_kbps = round(kbps)
            best_stats = stats
        else:
            break

    return best_rate, best_sps, best_kbps, best_stats


def run_endurance(scpi, ser, iface, fmt, channels, freq, label, dur=60, obdiag=None):
    """Run at a given rate for extended duration, report all stats."""
    print(f"    Endurance: {label} @ {freq}Hz for {dur}s ...")
    sps, kbps, stats, leaked = run_test(scpi, ser, iface, fmt, channels, freq, dur, obdiag=obdiag)

    qd = stats.get('QueueDroppedSamples', 0)
    eos = stats.get('EosOverruns', 0)
    dio = stats.get('DioDroppedSamples', 0)
    enc_d = stats.get('EncoderDroppedSamples', 0)
    enc_f = stats.get('EncoderFailures', 0)
    timer = stats.get('TimerISRCalls', 0)
    total = stats.get('TotalSamplesStreamed', 0)
    invariant = abs(timer - (total + qd))

    status = "LEAK" if leaked else "CLEAN"
    print(f"    {sps:>7.0f} sps  {kbps:>6.0f} KB/s  drops={qd}  eos={eos}  "
          f"dio={dio}  encDrop={enc_d}  encFail={enc_f}  inv={invariant}  {status}")
    return {
        'sps': round(sps), 'kbps': round(kbps), 'leaked': leaked,
        'queue_drops': qd, 'eos_overruns': eos, 'dio_drops': dio,
        'enc_drops': enc_d, 'enc_fails': enc_f, 'timer_isr': timer,
        'total_samples': total, 'invariant_delta': invariant,
    }


def main():
    parser = argparse.ArgumentParser(description='Overnight streaming characterization')
    parser.add_argument('port', nargs='?', default='/dev/ttyACM0')
    parser.add_argument('--duration', type=int, default=10,
                        help='Seconds per rate step in ceiling sweep (default: 10)')
    parser.add_argument('--endurance', type=int, default=60,
                        help='Seconds per endurance test at ceiling (default: 60)')
    parser.add_argument('--skip-endurance', action='store_true',
                        help='Skip endurance phase, only do ceiling sweep')
    parser.add_argument('--sd', action='store_true',
                        help='Include SD card tests (requires SD card inserted)')
    args = parser.parse_args()

    device = NyquistDevice.connect(args.port)
    scpi = ReliableSCPI(device)
    ser = device._comm._serial

    scpi.command('SYST:POW:STAT 1', 2)
    time.sleep(2)
    idn = scpi.query_kv('*IDN?')
    print(f"Device: {idn}")
    started = datetime.now()
    print(f"Started: {started.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Ceiling sweep: {args.duration}s per step")
    print(f"Endurance: {args.endurance}s per config")

    # ── Interface × Format matrix ────────────────────────────────
    interface_configs = [
        (0, 0, "USB PB"),
        (0, 2, "USB CSV"),
    ]
    if args.sd:
        interface_configs.extend([
            (2, 0, "SD PB"),
            (2, 2, "SD CSV"),
        ])

    all_results = []

    # ── Phase 1: Ceiling Sweep ───────────────────────────────────
    print(f"\n{'#'*70}")
    print(f"# PHASE 1: CEILING SWEEP (NoCap, fullscale test pattern)")
    print(f"# {len(interface_configs)} interfaces × {len(CHANNEL_CONFIGS)} channel configs")
    print(f"# = {len(interface_configs) * len(CHANNEL_CONFIGS)} configurations")
    print(f"{'#'*70}")

    for iface, fmt, iface_label in interface_configs:
        print(f"\n{'='*70}")
        print(f"  {iface_label}")
        print(f"{'='*70}")

        for ch_label, channels, obdiag_override in CHANNEL_CONFIGS:
            label = f"{iface_label} {ch_label}"
            print(f"\n  {label} ({len(channels)}ch):")

            ceiling, sps, kbps, stats = find_ceiling(
                scpi, ser, iface, fmt, channels, label, args.duration, obdiag=obdiag_override)

            result = {
                'phase': 'ceiling',
                'interface': iface_label,
                'channels': ch_label,
                'num_channels': len(channels),
                'ceiling_hz': ceiling,
                'sps': sps,
                'kbps': kbps,
                'eos_overruns': stats.get('EosOverruns', 0),
                'dio_drops': stats.get('DioDroppedSamples', 0),
                'enc_drops': stats.get('EncoderDroppedSamples', 0),
                'enc_fails': stats.get('EncoderFailures', 0),
                'timer_isr': stats.get('TimerISRCalls', 0),
            }
            all_results.append(result)
            print(f"    >>> Ceiling: {ceiling} Hz, {sps} sps, {kbps} KB/s")

    # ── Phase 1 Summary ──────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  CEILING SUMMARY")
    print(f"{'='*70}")
    print(f"  {'Config':<25s}  {'Ceiling':>8s}  {'SPS':>7s}  {'KB/s':>6s}  {'EOS':>5s}")
    print(f"  {'-'*55}")
    for r in all_results:
        if r['phase'] == 'ceiling':
            label = f"{r['interface']} {r['channels']}"
            print(f"  {label:<25s}  {r['ceiling_hz']:>7d}   {r['sps']:>7d}  "
                  f"{r['kbps']:>5d}  {r['eos_overruns']:>5d}")

    # ── Phase 2: Endurance ───────────────────────────────────────
    if not args.skip_endurance:
        print(f"\n{'#'*70}")
        print(f"# PHASE 2: ENDURANCE ({args.endurance}s at each ceiling)")
        print(f"{'#'*70}")

        ceiling_results = [r for r in all_results if r['phase'] == 'ceiling' and r['ceiling_hz'] > 0]

        for r in ceiling_results:
            iface_label = r['interface']
            ch_label = r['channels']
            ceiling = r['ceiling_hz']
            label = f"{iface_label} {ch_label}"

            # Find matching interface config
            iface = fmt = None
            for i, f, lbl in interface_configs:
                if lbl == iface_label:
                    iface, fmt = i, f; break
            if iface is None:
                continue

            # Find matching channel config
            channels = None
            obdiag_override = None
            for cl, chs, obd in CHANNEL_CONFIGS:
                if cl == ch_label:
                    channels = chs; obdiag_override = obd; break
            if channels is None:
                continue

            print(f"\n  {label}:")
            endurance = run_endurance(
                scpi, ser, iface, fmt, channels, ceiling, label, args.endurance,
                obdiag=obdiag_override)

            all_results.append({
                'phase': 'endurance',
                'interface': iface_label,
                'channels': ch_label,
                'num_channels': len(channels),
                'ceiling_hz': ceiling,
                'duration_s': args.endurance,
                **endurance,
            })

    # ── Save results ─────────────────────────────────────────────
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    os.makedirs("benchmarks", exist_ok=True)
    csv_file = f"benchmarks/overnight_{ts}.csv"

    fieldnames = [
        'phase', 'interface', 'channels', 'num_channels', 'ceiling_hz',
        'sps', 'kbps', 'eos_overruns', 'dio_drops', 'enc_drops', 'enc_fails',
        'timer_isr', 'total_samples', 'queue_drops', 'invariant_delta',
        'leaked', 'duration_s',
    ]
    with open(csv_file, 'w', newline='') as f:
        writer = csvmod.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(all_results)

    elapsed = datetime.now() - started
    print(f"\n{'='*70}")
    print(f"  COMPLETE")
    print(f"  Elapsed: {elapsed}")
    print(f"  Results: {csv_file}")
    print(f"  Configs tested: {len(all_results)}")
    print(f"{'='*70}")

    device.disconnect()


if __name__ == "__main__":
    main()
