# Integration suite — perf, throughput, and stress

Mirrored from
[`daqifi/daqifi-python-test-suite`](https://github.com/daqifi/daqifi-python-test-suite)
so the port's regression harness ships with the library it exercises.
These scripts run against a connected DAQiFi Nyquist device and produce
quantitative baselines for streaming throughput, USB CDC ceilings,
WiFi/SD/USB stress cycles, and silent-loss observability.

The mirror imports `daqifi-python-core` exclusively (`from daqifi import
NyquistDevice`); the legacy `sys.path.insert` shims that the upstream
copies use to find the core in a sibling directory have been stripped.

## Layout

- `test_harness.py` — shared helpers (`ReliableSCPI`, `FastReader`,
  `StreamingMeasurement`, `device_reset`). Read its module docstring
  before writing a new test — the streaming-rate measurement and the
  ≤1ms-interval reader are load-bearing for any test that streams.
- `test_*.py` / `run_all_rate_tests.py` — runnable scripts (NOT
  pytest tests). Each is invoked directly with hardware attached.
- `test_config.yaml.template` — copy to `test_config.yaml` and fill
  in your local port / WiFi credentials before running the
  config-driven tests.

## Why these aren't pytest-collected

Several scripts open the serial port at module top level (e.g.
`device = NyquistDevice.connect('/dev/ttyACM0')`), so pytest's
collection phase would attempt the connection and either hang or
fail every collection on a machine without hardware. A `conftest.py`
in this directory tells pytest to skip the whole tree.

## Running a script

```bash
# From repo root, with the device on /dev/ttyACM0 (Linux/WSL) or COM9 (Windows):
cd tests/integration/suite
python3 test_max_throughput.py
```

Most scripts accept config flags or read `test_config.yaml`. Inspect
each file's top-of-module argparse / config section before invoking.

## Canonical streaming pattern

```python
from daqifi import NyquistDevice
from test_harness import ReliableSCPI, StreamingMeasurement

device = NyquistDevice.connect("/dev/ttyACM0")
scpi = ReliableSCPI(device)
m = StreamingMeasurement(scpi, scpi._serial)
m.start(freq=5000, duration=10, is_csv=True)
print(f"SPS: {m.pc_sps}, KB/s: {m.pc_kbps}, leaked: {m.leaked}")
```

## Resyncing from upstream

When the upstream suite gains new files worth pulling in:

```bash
cp ~/GitHub/daqifi-python-test-suite/test_<new>.py \
   tests/integration/suite/test_<new>.py
sed -i '/sys\.path\.insert/d; /^# Add parent paths for imports$/d' \
   tests/integration/suite/test_<new>.py
python3 -m py_compile tests/integration/suite/test_<new>.py
```
