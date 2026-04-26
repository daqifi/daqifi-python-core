# DAQiFi Python API

A clean, Pythonic API for controlling DAQiFi Nyquist devices (NQ1, NQ2, NQ3).

## Features

- **Device Discovery**: Automatically find connected devices
- **Property-Based Control**: Intuitive, Pythonic interface
- **Data Streaming**: Real-time data acquisition with multiple formats (JSON, CSV, ProtoBuf)
- **Analog Outputs**: Control DAC channels (NQ3 only, 8 channels, 0-10V)
- **Analog Inputs**: Configure ADC channels (8-16 channels depending on variant)
- **Digital I/O**: Control 16 digital I/O channels
- **Power Management**: Control device power states
- **WiFi Connectivity**: Configure and test WiFi TCP streaming
- **Comprehensive Test Suite**: YAML-configured test suite for complete device validation
- **Type Hints**: Full type annotation for IDE support
- **Context Managers**: Automatic cleanup with `with` statement

## Installation

### For Local Development

```bash
cd docs/daqifi
pip install -e .
```

### Dependencies

```bash
pip install pyserial pyyaml
```

For testing and data analysis (optional):
```bash
pip install pandas matplotlib
```

## Quick Start

```python
from daqifi import NyquistDevice, DeviceFinder

# Discover devices
devices = DeviceFinder.discover()
print(f"Found {len(devices)} device(s)")

# Connect to first device
device = NyquistDevice.connect(devices[0])

# Power up
device.power_up()

# Control DAC (NQ3 only)
device.analog_outputs[0].voltage = 5.0
device.analog_outputs[0].enabled = True

# Configure ADC
device.analog_inputs[0].range = "10V"
device.analog_inputs[0].enabled = True

# Control DIO
device.digital_io[0].direction = "output"
device.digital_io[0].state = True

# Disconnect
device.disconnect()
```

## Usage Examples

### Device Discovery

```python
from daqifi import DeviceFinder, BoardVariant

# Find all devices
devices = DeviceFinder.discover()

# Find only NQ3 devices
nq3_devices = DeviceFinder.discover(variant=BoardVariant.NQ3)

# Find first available device
device_info = DeviceFinder.discover_first()
```

### Context Manager

```python
from daqifi import NyquistDevice

# Automatic cleanup
with NyquistDevice.connect("/dev/ttyACM0") as device:
    device.power_up()
    device.analog_outputs[0].voltage = 5.0
    # Device automatically disconnects on exit
```

### DAC Control (NQ3 Only)

```python
# Single channel
device.analog_outputs[0].voltage = 5.0
device.analog_outputs[0].enabled = True

# Multiple channels
device.analog_outputs.set_voltages({
    0: 5.0,
    1: 3.3,
    2: 1.8
})

# Enable all
device.analog_outputs.enable_all()

# Read back
voltage = device.analog_outputs[0].voltage
```

### ADC Configuration

```python
# Single channel
device.analog_inputs[0].range = "10V"
device.analog_inputs[0].enabled = True

# Multiple channels
device.analog_inputs.configure_channels(
    channels=[0, 1, 2],
    range="5V",
    enabled=True
)

# Disable all
device.analog_inputs.disable_all()
```

### Digital I/O

```python
# Configure as output
device.digital_io[0].direction = "output"
device.digital_io[0].state = True  # High

# Configure as input
device.digital_io[1].direction = "input"
state = device.digital_io[1].state  # Read input

# Bulk operations
device.digital_io.configure_outputs([0, 1, 2])
device.digital_io.set_states({
    0: True,
    1: False,
    2: True
})

# Read all
states = device.digital_io.read_all()
```

### Power Management

```python
from daqifi import PowerState

# Power up (required for DAC)
device.power_up()

# Check power state
if device.power_state == PowerState.POWERED_UP:
    print("Device fully powered")

# Power down
device.power_down()
```

### Data Streaming

The API supports three streaming formats:
- **JSON** (format=1): Human-readable, fully parsed
- **CSV** (format=2): Efficient line-based format
- **ProtoBuf** (format=0): Binary format for high-throughput

```python
# Basic streaming (JSON format)
stream = device.create_stream(channels=[0, 1, 2], sample_rate=1000, format=1)
stream.start()
samples = stream.read(num_samples=100)
stream.stop()

for sample in samples:
    print(f"Timestamp: {sample.timestamp}, Values: {sample.values}")

# CSV format streaming
stream = device.create_stream(channels=[0], sample_rate=10, format=2)
stream.start()
samples = stream.read(num_samples=5, timeout=3.0)
stream.stop()

# ProtoBuf format (binary)
stream = device.create_stream(channels=[0], sample_rate=10, format=0)
stream.start()
samples = stream.read(num_samples=5, timeout=3.0)
stream.stop()

# Context manager (automatic start/stop)
with device.create_stream(channels=[0, 1], sample_rate=1000, format=1) as stream:
    samples = stream.read(num_samples=100, timeout=10.0)

# Callback-based streaming
def on_sample(sample):
    print(f"Received: {sample}")

stream.start(callback=on_sample)
time.sleep(10)
stream.stop()

# WiFi TCP Streaming
# See testing/suites/README.md for WiFi configuration examples
```


## API Reference

### Main Classes

- **`NyquistDevice`**: Main device controller
- **`DeviceFinder`**: Device discovery
- **`AnalogOutputChannel`**: Single DAC channel
- **`AnalogInputChannel`**: Single ADC channel
- **`DigitalIOChannel`**: Single DIO channel

### Enums

- **`BoardVariant`**: Device variants (NQ1, NQ2, NQ3)
- **`PowerState`**: Power states (STANDBY, POWERED_UP, POWERED_UP_EXT_DOWN)

### Exceptions

- **`DAQiFiError`**: Base exception
- **`ConnectionError`**: Connection failed
- **`DeviceNotFoundError`**: No device found
- **`CommandError`**: SCPI command failed
- **`ConfigurationError`**: Invalid configuration
- **`FeatureNotSupportedError`**: Feature not available
- **`PowerStateError`**: Power state error

## Device Variants

### NQ1
- 16 ADC channels (MC12bADC)
- 16 DIO channels
- No DAC

### NQ2
- 16 ADC channels (assumed)
- 16 DIO channels
- No DAC

### NQ3
- 8 ADC channels (AD7609)
- 8 DAC channels (DAC7718, 0-10V)
- 16 DIO channels

## Testing

### Comprehensive Test Suite

A complete YAML-configured test suite is available in `testing/suites/`:

```bash
cd testing/suites

# Copy template and configure
cp test_config.yaml.template test_config.yaml
# Edit test_config.yaml with your WiFi credentials

# Run all tests (USB CDC + WiFi)
python3 comprehensive_test.py

# Run USB-only tests (quick test)
python3 comprehensive_test.py test_config_usb_only.yaml
```

**Test Coverage:**
- ✓ USB CDC connection and SCPI commands
- ✓ Power management
- ✓ ADC/DIO configuration
- ✓ USB streaming (JSON, CSV, ProtoBuf)
- ✓ WiFi configuration (STA/AP modes)
- ✓ WiFi TCP streaming (all formats)

See [testing/suites/README.md](testing/suites/README.md) for detailed documentation.

### Individual Examples

Run example scripts:

```bash
cd examples

# Device discovery
python3 device_discovery.py

# Basic ADC
python3 basic_adc.py

# Basic streaming
python3 streaming_basic.py
```

## Architecture

The API follows a layered architecture:

```
Public API (daqifi.*)
  ├── device.py           - NyquistDevice
  ├── discovery.py        - DeviceFinder
  ├── channels.py         - Channel classes
  └── exceptions.py       - Exception hierarchy

Internal Layer (daqifi._internal.*)
  ├── comm.py             - Serial/SCPI communication
  └── scpi.py             - SCPI command definitions

Testing Extensions (daqifi.testing.*)
  ├── framework.py        - Test runner
  └── suites/             - Pre-built test suites
```

## Alignment with daqifi-core

This Python API mirrors the C# daqifi-core library:

| C# (daqifi-core) | Python (daqifi-python) |
|------------------|------------------------|
| `NyquistDevice` | `NyquistDevice` |
| `DeviceFinder` | `DeviceFinder` |
| `AnalogOutputChannel` | `AnalogOutputChannel` |
| `AnalogInputChannel` | `AnalogInputChannel` |
| `DigitalIOChannel` | `DigitalIOChannel` |

The C#-to-Python port itself is tracked under `.portomatic/` — see
**[Port maintenance](#port-maintenance-portomatic)** below for the tooling
that catalogs every C# symbol's Python counterpart and audits the port for
drift.

## Port maintenance (portomatic)

The port from `daqifi-core` (C#) to this repo is catalogued at the
**method level** in `.portomatic/map/<Shard>.yaml` and maintained
via the [portomatic](https://github.com/cptkoolbeenz/portomatic)
CLI:

```bash
# Refresh proposed pairings for one shard (interactive review).
portomatic map suggest --shard Network \
  --upstream-ir /tmp/up.ir.json --downstream-ir /tmp/dn.ir.json --apply

# Audit every shard for drift across 5 categories.
portomatic map audit --downstream-ir /tmp/daqifi.ir.json --downstream .

# Strict mode for CI gates — exits 1 if any drift is reported.
portomatic map audit --downstream-ir /tmp/daqifi.ir.json --strict
```

See **[`.portomatic/README.md`](./.portomatic/README.md)** for the
full workflow with extract commands + the categorical drift table.

**Historical note** — earlier in the port, this repo carried two
pilot scripts (`scripts/portomatic_generate_map.py` /
`portomatic_audit.py`) that reimplemented the equivalent features.
Once `portomatic map suggest` and `portomatic map audit` shipped
upstream (see
[`portomatic#155`](https://github.com/cptkoolbeenz/portomatic/pull/155)),
the pilots were retired and the maps were migrated to the official
schema vocabulary. **Don't write new helper scripts** without first
checking
[`portomatic/docs/cli-reference.md`](https://github.com/cptkoolbeenz/portomatic/blob/main/docs/cli-reference.md)
to confirm the feature isn't already there.

## Known Issues

### Firmware Bug: Streaming Without Enabled Channels
Starting streaming without any enabled ADC channels causes a firmware crash (TLB exception). The Python API checks for enabled channels before starting streaming and raises a `ConfigurationError` if no channels are enabled.

**GitHub Issue:** [#132](https://github.com/daqifi/daqifi-nyquist-firmware/issues/132)

**Workaround:** Always enable at least one ADC channel before streaming:
```python
device.analog_inputs[0].enabled = True
stream = device.create_stream(channels=[0], sample_rate=10)
stream.start()
```

### WiFi TCP Connection Timing
When testing multiple streaming formats over WiFi TCP, allow 1 second between tests for the device to clean up connections. The comprehensive test suite handles this automatically.

## Troubleshooting

### Device Not Found
```
ConnectionError: Failed to connect to device
```
**Solutions:**
- Check device is connected: `ls -la /dev/ttyACM*`
- Verify correct port in code
- Check USB cable and device power
- On WSL: Use `usbipd` to attach device to WSL

### Permission Denied
```
PermissionError: [Errno 13] Permission denied: '/dev/ttyACM0'
```
**Solutions:**
- Add user to dialout group: `sudo usermod -a -G dialout $USER`
- Log out and back in for group changes to take effect
- Or run with sudo (not recommended)

### WiFi Connection Failed
```
No IP address - WiFi connection failed
```
**Solutions:**
- Verify SSID and password are correct
- Check WiFi network is in range
- Ensure security mode matches (0=Open, 3=WPA)
- Device must be POWERED_UP for WiFi to work
- Try increasing connection timeout in test config

### Streaming Timeout
```
Timeout waiting for samples
```
**Solutions:**
- Ensure at least one ADC channel is enabled
- Check device power state is POWERED_UP
- Verify sample rate is valid for device
- Check serial port connection is stable

## Architecture

`NyquistDevice` (the API documented above) is the high-level
convenience layer. A lower-level set of building blocks ported
directly from the C# `Daqifi.Core` library (transports, device
finders, `DaqifiStreamingDevice`, SCPI command factories) is also
available under `daqifi.transport`, `daqifi.devices`,
`daqifi.device_finders`, and `daqifi._internal.scpi_commands`. See
[PORT_NOTES.md](./PORT_NOTES.md) for the layer diagram and module
index.

## License

MIT License - Copyright (c) DAQiFi

## Support

For issues or questions:
- Create an issue in the [GitHub repository](https://github.com/daqifi/daqifi-nyquist-firmware/issues)
- Contact DAQiFi support
