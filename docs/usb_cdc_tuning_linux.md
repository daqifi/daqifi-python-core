# USB CDC tuning on Linux / WSL2

Recipe for getting the best USB-CDC streaming throughput out of a
DAQiFi NQ1 (or any device that uses Linux's `cdc_acm` kernel driver)
when host-side overhead — not the device — is the bottleneck.

Spun out of #18 §1.

## Background

`SerialStreamTransport.connect()` requests a 64 KiB OS-level RX/TX
buffer via pyserial's `set_buffer_size()`. On Windows that maps to
Win32 `SetupComm()` and the `usbser.sys` driver honors it. On Linux
the call raises `NotImplementedError` — pyserial catches it, the
debug-log line in `transport.py` records the no-op, and the kernel
keeps using its built-in `cdc_acm` defaults:

- One IN / one OUT URB pool (`NR_URBS = 16` per direction in modern
  kernels, hard-coded in `drivers/usb/class/cdc-acm.c`).
- URB size = device endpoint's `wMaxPacketSize` (64 bytes for
  Full-Speed USB CDC, 512 bytes for High-Speed).
- Effective host-side buffering = `NR_URBS × wMaxPacketSize` ≈
  1 KiB on Full-Speed devices.

That's small. At 50 kHz × 16 channels (≈4 MB/s sustained) the
window between the device emitting a burst and the consumer thread
draining it is roughly 0.25 ms before the kernel's internal queue
overflows and the device's USB writer drops bytes.

## How to verify what you're running

Run the inspector:

```bash
python3 scripts/cdc_acm_inspect.py
```

It prints:

- `cdc_acm` module presence + version
- `NR_URBS` (when exposed via `modinfo` parameters)
- Bound device paths and their `bMaxPacketSize` values from `lsusb -v`
- WSL2 vs native Linux detection
- Recommended next steps based on what it found

The inspector does NOT modify anything.

## Recommended modprobe configuration

Increasing `NR_URBS` is the only Linux-side knob that makes a
material difference for sustained streaming. Most distros ship
`cdc_acm` with `NR_URBS=16`; bumping to 64 quadruples the
host-side cushion. Larger values (128, 256) yield diminishing
returns and cost kernel memory linearly per attached device.

Create `/etc/modprobe.d/cdc_acm.conf`:

```
# Increase cdc_acm URB pool from default 16 to 64 for high-rate
# USB-CDC streaming (DAQiFi NQ1 at >10 kHz × multi-channel).
# Costs roughly 64 × wMaxPacketSize × 2 bytes per attached device.
options cdc_acm nr_urbs=64
```

(Some kernel builds expose the option as `nr_urbs`, others as
`NR_URBS`, others bake it in entirely. Check `modinfo cdc_acm`
first — the inspector does this for you.)

Apply:

```bash
sudo rmmod cdc_acm
sudo modprobe cdc_acm
# Reattach any DAQiFi devices (unplug/replug or usbipd attach).
```

Survives reboot because of the `modprobe.d` config.

### WSL2 specifics

WSL2 bundles its own kernel. As of WSL2 kernel 6.6+ the build
includes `cdc_acm` but does NOT expose `nr_urbs` as a module
parameter — it's a compile-time constant. The `modprobe.d` recipe
above will load harmlessly but won't change behavior.

If the kernel happens to expose the parameter (rare; depends on
your WSL2 build), the same recipe applies. If not, the practical
options are:

1. **Use Windows native** for high-rate streaming. The Win32
   `usbser.sys` path with our 64 KiB `SetupComm` buffer is well-
   tuned and not a bottleneck below 50 kHz × 16 ch.
2. **Build a custom WSL2 kernel** with a higher `NR_URBS`. Out of
   scope for this doc.
3. **Stay below the threshold**. From the benchmark matrix
   (`scripts/benchmark_streaming.py`) drops only become significant
   above ~10 kHz × 16 channels on stock WSL2 kernels.

## Validating the change

Before and after the modprobe change, run the benchmark:

```bash
DAQIFI_BENCHMARK_PORT=/dev/ttyACM<N> \
DAQIFI_BENCHMARK_EXPECTED_SN=<your-sn> \
    python3 scripts/benchmark_streaming.py \
    --duration 5 \
    --rates 10000,50000 \
    --channels 16 \
    --buffers 65536 \
    --out /tmp/bench-after.json
```

The `yield_pct` column should improve at the highest rate × channel
combination if the change is doing anything. If yield is unchanged,
either the kernel doesn't honor `nr_urbs` for your build, or the
bottleneck is elsewhere (Python parser, serial driver, USB host
controller).

## Measured baseline (WSL2, stock kernel, no nr_urbs exposed)

Numbers below are from N=13 iterations of the full matrix on this
host: WSL2 + usbipd-win, `cdc_acm` from the stock WSL2 kernel,
DAQiFi NQ1 fw 3.4.6b1 over USB CDC. Each cell is yield_pct (parsed
PC-side frames ÷ expected frames × 100), so 100% = no drops, lower
= drops.

| rate (Hz) | 1 channel | 16 channels |
|---:|---:|---:|
|     100 | ~102% | ~102% |
|   1 000 | ~102% | ~102% |
|   5 000 | ~102% | ~102% |
|  10 000 | ~103% | **~51%** |
|  50 000 | **~27%** | **~10%** |

(Yields slightly above 100% are protobuf-stream warm-up frames
counted past the wall-clock window.)

Headline takeaways:

- **Buffer size makes ~zero difference at the bottleneck**. Across
  4 KiB, 64 KiB, and 256 KiB Python read chunks, every cell is
  identical to within σ < 0.5%. The `set_buffer_size()` call from
  PR #19 is a no-op on Linux/WSL2 (pyserial raises
  `NotImplementedError`), and the matching consumer-side chunk
  size also doesn't matter once the kernel URB pool is full.
- **Soak (120 s) yields the same as 5 s and 30 s** at the worst
  combo (50 kHz × 16 ch). The bottleneck is steady-state, not a
  warm-up artifact.
- **Cap is firmware-side, not host-side.** Originally suspected to
  be the cdc_acm URB pool, but the Windows-native cross-check
  below ruled that out.

## Cross-check: WSL2 vs Windows native

Re-ran the same matrix on Windows native (Python 3.12, pyserial
3.5 against `usbser.sys`, COM9, no usbipd) for N=3 iterations.
The hypothesis from PR #19 — that Windows would do better because
`pyserial.set_buffer_size()` calls Win32 `SetupComm()` which
`usbser.sys` honors — is **not supported by the data**:

| rate (Hz) | ch | WSL2 yield (N=13) | Win yield (N=3) | Δ (Win − WSL) |
|---:|---:|---:|---:|---:|
|     100 |  1 | 102.0% | 100.8% | −1.2% |
|   1 000 |  1 | 102.6% | 100.4% | −2.2% |
|   5 000 |  1 | 102.6% | 100.7% | −1.9% |
|  10 000 |  1 | 102.7% | 100.6% | −2.1% |
|  50 000 |  1 |  26.6% |  26.0% | −0.6% |
|     100 | 16 | 101.9% | 100.6% | −1.3% |
|   1 000 | 16 | 102.1% | 100.0% | −2.1% |
|   5 000 | 16 | 102.9% | 100.6% | −2.3% |
|  10 000 | 16 |  51.2% |  50.4% | −0.8% |
|  50 000 | 16 |  10.2% |  10.0% | −0.2% |

Windows is consistently 0.2–2.3% LOWER than WSL2 — well within
measurement noise (the 1–2% gap is likely WSL2 counting protobuf
warm-up frames past the wall-clock window, since WSL2 yields
trend slightly above 100% on combos that should clip at 100%).
Crucially: **the bottleneck combos drop the same amount on both
platforms**: 50 kHz × 16 ch yields 10.0–10.2% on either, soak
behavior identical.

This means the cap is **the DAQiFi NQ1's USB-CDC writer in
firmware**, not the host driver, kernel URB pool, or Python-side
buffer. PR #19's `set_buffer_size()` call is harmless on Windows
but doesn't move the needle at the rates that matter, and the
modprobe.d recipe in this doc would similarly do nothing if WSL2
ever exposed `nr_urbs`.

Practical guidance:

| target rate | guidance |
|---|---|
| ≤ 10 kHz × 1 ch | works on WSL2 and Windows native |
| ≤ 5 kHz × 16 ch | works on WSL2 and Windows native |
| 10 kHz × 16 ch | ~50% drops on either — split into two 5 kHz × 8 ch sessions, or fix in firmware |
| ≥ 50 kHz any width | not viable on either OS — firmware-side fix required (firmware ticket needed) |

The "switch to Windows" remediation in earlier drafts of this
doc was based on an unvalidated hypothesis; it doesn't help.

## Out of scope here

- Telemetry / drop detection — landed in #68 (`consumer.stats`).
- Windows `set_buffer_size()` validation — tracked separately
  in #77.
- Auto-tune — only worth pursuing if the static recommendation
  underperforms across the matrix; defer until the benchmark
  results say so.
