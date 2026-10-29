# DAQiFi Python API

A clean, Pythonic API for controlling DAQiFi Nyquist devices (NQ1, NQ2, NQ3).

## Features

- **Device Discovery**: Automatically find connected devices, including multi-NIC UDP discovery for hosts with multiple network interfaces
- **Property-Based Control**: Intuitive, Pythonic interface
- **Data Streaming**: Real-time data acquisition with multiple formats (JSON, CSV, ProtoBuf)
- **CSV Export**: Stream samples to CSV with absolute or relative timestamps and optional rolling-window averaging
- **Analog Outputs**: Control DAC channels (NQ3 only, 8 channels, 0-10V)
- **Analog Inputs**: Configure ADC channels (8-16 channels depending on variant)
- **Digital I/O**: Control 16 digital I/O channels
- **Power Management**: Control device power states
- **WiFi Connectivity**: Configure and test WiFi TCP streaming
- **SD Card Operations**: Inspect, transfer, and delete on-device SD card files with structured error types (busy / not present / filesystem / generic)
- **Error Queue Drain**: Inspect and drain the device's SCPI error queue (`drain_errors`) to surface silent failures
- **Firmware Updates**: PIC32 + WIFI module flashing with optional post-reconnect application-readiness probe
- **Comprehensive Test Suite**: YAML-configured test suite for complete device validation
- **Type Hints**: Full type annotation for IDE support
- **Context Managers**: Automatic cleanup with `with` statement

## Installation

### For Local Development

```bash
# From the repo root
pip install -e .
```

### Dependencies

Runtime: `pyserial`, `pyyaml`, `protobuf` (declared in `setup.py`).

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
# Single channel; an output drives the voltage last set
device.analog_outputs[0].voltage = 5.0

# Multiple channels
device.analog_outputs.set_voltages({
    0: 5.0,
    1: 3.3,
    2: 1.8
})

# Stop driving outputs (there is no DAC enable/disable on current firmware)
device.analog_outputs.set_voltages({0: 0.0, 1: 0.0, 2: 0.0})

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

### CSV Export

Stream samples directly to a CSV file via `daqifi.export.CsvExporter`.
The exporter is decoupled from storage — implement
`daqifi.export.SampleSource` over whatever holds your samples
(in-memory list, SQLite, on-device SD card files, etc.).

```python
from daqifi.export import (
    CsvExporter, CsvExportOptions, ChannelDescriptor, SampleRow, SampleSource,
)

class InMemorySource(SampleSource):
    def __init__(self, channels, samples):
        self._channels = channels
        self._samples = samples
    def get_channels(self): return self._channels
    def get_sample_count(self): return len(self._samples)
    def stream_samples(self): yield from self._samples

source = InMemorySource(
    channels=[ChannelDescriptor("NQ1", "9090...", "AI0", "Analog")],
    samples=[SampleRow(timestamp_ticks=t, channel_key="NQ1:9090...:AI0", value=v)
             for t, v in enumerate(values)],
)

with open("export.csv", "w") as f:
    CsvExporter().export(
        source, f,
        CsvExportOptions(use_relative_time=True, average_window=100),
    )
```

### SD Card Operations

```python
from daqifi.exceptions import (
    SdCardBusyException, SdCardNotPresentException,
    SdCardFilesystemException, SdCardOperationException,
)

try:
    files = device.request_sd_card_file_list()
    for f in files:
        print(f.name, f.size_bytes)
except SdCardBusyException:
    print("SD logging in progress — stop it first")
except SdCardNotPresentException:
    print("No SD card inserted")
except SdCardFilesystemException as e:
    print(f"Filesystem error: {e}")
except SdCardOperationException as e:
    print(f"Generic SD failure: {e}")
```

Listing, downloading and deleting refuse while this session is logging to
the SD card; stop logging first. `is_logging_to_sd_card` and
`get_sd_logging_state()` report only recordings started through this
device object. The firmware has no query for it, so a recording started by
another client, another session or an earlier process reads `False`.

### Error Queue Inspection

The device maintains a SCPI error queue. ``drain_errors`` empties
it and returns the popped errors so callers can log, raise, or
discard them — much more useful than the legacy "fire and forget"
``clear_errors`` for diagnosing silent failures (`-101` invalid
character, `-200` execution error, etc.).

```python
errors = device.drain_errors()        # returns list[str]
if errors:
    print("Pre-existing errors before our session:")
    for e in errors:
        print(f"  {e}")
# Subsequent commands now run against a known-clean error queue.
```

### Firmware Update with Application-Readiness Probe

`FirmwareUpdateServiceOptions` carries the timeouts, retry policy,
and (new) post-reconnect readiness probe. The probe is opt-in:
when set, the service polls it after the post-flash serial
reconnect to confirm the PIC32 application is actually ready to
answer protobuf queries (not just that the serial transport
reopened).

```python
from daqifi.firmware.update_options import FirmwareUpdateServiceOptions

opts = FirmwareUpdateServiceOptions()

def app_ready(device, deadline):
    try:
        device.send("SYST:INFo?")     # any quick status query
        return True
    except Exception:
        return False

opts.post_reconnect_readiness_probe = app_ready
opts.post_reconnect_readiness_timeout = 30.0   # bound the wait
opts.post_reconnect_readiness_retry_delay = 0.5
opts.validate()                                # raises ValueError on misconfig

# Pass `opts` into DefaultFirmwareUpdateService(...) along with the
# HID transport / download service / process runner. See
# tests/portomatic-generated/test_default_firmware_update_service.py
# for a full wiring example.
```

### Several boards on one host: which bootloader gets flashed

A PIC32 update reboots the board into its HID bootloader
(`SYSTem:FORceBoot`) and then has to find that bootloader among every
HID device with the same VID/PID. Bootloaders report no serial number,
so the service first makes sure the choice is trivial, then narrows it:

1. **One board.** Before sending anything, the service counts the DAQiFi
   boards on USB: serial ports with the application's VID/PID
   (`application_vendor_id`/`application_product_id`, 04D8:F794) plus
   bootloaders already enumerated. Serial ports are listed before and
   after the bootloader scan, so a board switching modes in between is
   still counted. With more than one board, or when serial ports can't be
   listed at all, the update is refused with a message saying why. Disconnect every board except
   the one being updated, including any left in bootloader mode, and
   retry. A board reached over WiFi only isn't on USB and isn't counted.
2. **Snapshot.** Bootloaders already enumerated before `FORceBoot`
   belong to other boards and are never selected.
3. **USB port.** On Linux, a bootloader must appear on the same physical
   USB port as the board's serial port, read from sysfs. A board that
   enters its bootloader on another port during the wait is ignored.

The service then opens the selected bootloader by device path, and
refuses if more than one candidate remains. If the board's own serial
port drops after `FORceBoot` and then comes back, the board rebooted
into its application, which is what a board built without the bootloader
does. The update stops there rather than take whatever bootloader
appears next.

**Residual risk.** Step 1 runs once, at the start. A second board
plugged in (or attached over usbipd) *during* the update isn't caught by
it, nor is one that is mid-reboot (visible neither as a serial port nor
as a bootloader) at the moment of the check. Step 3 is unavailable on Windows, on macOS, and under WSL2 + usbipd,
where the virtual port can change when the board re-attaches. There, a
board that arrives and enters its bootloader in the seconds between
`FORceBoot` and enumeration is indistinguishable from the target. Don't
connect other boards while an update runs; on a shared multi-board host,
take the bench lock for the duration of an update.

### Bootloader that sat idle stops answering

Once a board is in its HID bootloader, send it its first command promptly.
If the bootloader sits idle, Windows selectively suspends it about 5–6 s
after it enumerates (11 bench trials on a host with USB selective suspend
enabled: D0 to D3 between 5.0 and 6.2 s; a first command at 5.4 s worked,
one at 6.3 s did not). On the way back from suspend the bootloader's
USB stack sees a bus reset without the SET_CONFIGURATION that should follow,
so its endpoints stay disabled. Writes are then accepted but get no reply,
or fail outright. The bootloader source documents this (its #568 note in
`datastream_usb_hid.c`; tracked as daqifi-nyquist-firmware #1172), and
recovery needs a fresh enumeration: the host closes and reopens the device,
or the board re-enters the bootloader.

`DefaultFirmwareUpdateService` sends its first command (a version request)
about 50 ms after it sees the bootloader, so a normal update stays well
inside that window. A host that takes several seconds between enumerating
the bootloader and connecting to it (a slow HID stack, an antivirus scan, a
debugger breakpoint) can miss it. If the bootloader has already sat idle, for example
after an update that was interrupted while waiting, the version request
fails and the update stops **before ERASE**. The application is untouched.
Bring the board out of the bootloader (power-cycle, or re-enter it and
retry promptly) and run the update again. The updater does not yet reopen
the device and retry by itself.

### Multi-NIC WiFi Discovery

On hosts with multiple network interfaces (Ethernet + WiFi, VPN
adapters, virtual NICs), the lower-level `WiFiDeviceFinder`
enumerates real NICs and binds the UDP discovery socket
per-interface so devices reachable via any LAN-attached NIC are
found — not just those on the default route. Virtual adapters
(loopback, Tailscale tunnels, etc.) are skipped automatically.

```python
from daqifi.device_finders import WiFiDeviceFinder
from datetime import timedelta

finder = WiFiDeviceFinder()
infos = finder.discover(timeout=timedelta(seconds=5))

# Each result carries its origin NIC's IP for multi-homed setups:
for info in infos:
    print(f"{info.serial_number}  IP={info.ip_address}  "
          f"via NIC {info.local_interface_address}")
```


## API Reference

### Main Classes

High-level convenience layer:

- **`NyquistDevice`**: Main device controller
- **`DeviceFinder`**: Device discovery (USB CDC + multi-NIC WiFi UDP)
- **`AnalogOutputChannel`**: Single DAC channel
- **`AnalogInputChannel`**: Single ADC channel
- **`DigitalIOChannel`**: Single DIO channel

Lower-level building blocks ported from `Daqifi.Core`
(use these when the high-level API doesn't fit):

- **`DaqifiDevice` / `DaqifiStreamingDevice`** (`daqifi.devices`) — direct port of the C# device classes
- **`DaqifiDeviceFactory`** (`daqifi.devices`) — `connect_serial(port)`, `connect_tcp(host, port)`, `connect_from_device_info(info)`
- **`SerialStreamTransport` / `TcpStreamTransport`** (`daqifi.transport`)
- **`SerialDeviceFinder` / `WiFiDeviceFinder`** (`daqifi.device_finders`)

Firmware update + export modules:

- **`DefaultFirmwareUpdateService`** (`daqifi.firmware`) — PIC32 + WIFI flash
- **`FirmwareUpdateServiceOptions`** (`daqifi.firmware.update_options`) — timeouts, retry counts, readiness probe
- **`CsvExporter` / `CsvExportOptions`** (`daqifi.export`) — sample → CSV writer
- **`ChannelDescriptor` / `SampleRow` / `SampleSource`** (`daqifi.export`) — exporter data model

### Enums

- **`BoardVariant`**: Device variants (NQ1, NQ2, NQ3)
- **`PowerState`**: Power states (STANDBY, POWERED_UP, POWERED_UP_EXT_DOWN)
- **`FirmwareUpdateState`** (`daqifi.enums`): firmware update lifecycle states

### Exceptions

Base + general:

- **`DAQiFiError`**: Base exception
- **`ConnectionError`**: Connection failed
- **`DeviceNotFoundError`**: No device found
- **`CommandError`**: SCPI command failed (carries the SCPI error code + raw response when available)
- **`ConfigurationError`**: Invalid configuration parameter
- **`FeatureNotSupportedError`**: Feature not available on this device variant
- **`PowerStateError`**: Device not in the required power state for the operation
- **`DeviceUnresponsiveError`**: TCP socket healthy but the device's SCPI listener never replied (typically after cumulative connect/disconnect cycles)

SD card operations (port of upstream PR #182):

- **`SdCardOperationException`**: Base for SD card failures; carries the raw response lines
- **`SdCardBusyException`**: SD logging is in progress; stop it before the operation
- **`SdCardNotPresentException`**: No card inserted
- **`SdCardFilesystemException`**: Filesystem-level failure (corrupt, unreadable, etc.)

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

The API has two layers:

**High-level convenience layer** — what most callers use:

```
daqifi/
  ├── device.py           - NyquistDevice
  ├── discovery.py        - DeviceFinder
  ├── channels.py         - Channel classes
  └── exceptions.py       - Exception hierarchy
```

**Lower-level building blocks** ported 1:1 from C# `Daqifi.Core`
(use these when the high-level API doesn't fit your case):

```
daqifi/
  ├── devices.py          - DaqifiDevice, DaqifiStreamingDevice, DaqifiDeviceFactory
  ├── transport.py        - SerialStreamTransport, TcpStreamTransport
  ├── device_finders.py   - SerialDeviceFinder, WiFiDeviceFinder
  ├── communication.py    - ProtobufMessageParser, ProtobufMessage
  ├── streaming.py        - StreamingChannelDispatcher
  ├── network.py          - WiFi/LAN configuration
  ├── sdcard.py           - SD card file operations
  ├── export.py           - CsvExporter + SampleSource
  ├── firmware/           - DefaultFirmwareUpdateService + bootloader, hex parser, etc.
  └── _internal/
        ├── comm.py       - SerialInterface (low-level SCPI I/O)
        └── scpi_commands.py - SCPI command factories (mirrors C# ScpiMessageProducer)
```

The package tree above is the module index.

## Alignment with daqifi-core

This Python API mirrors the C# daqifi-core library. Selected
correspondences (full machine-readable map under `.portomatic/map/`):

| C# (daqifi-core) | Python (daqifi-python-core) |
|---|---|
| `NyquistDevice` | `NyquistDevice` |
| `DeviceFinder` | `DeviceFinder` |
| `DaqifiDevice` / `DaqifiStreamingDevice` | `DaqifiDevice` / `DaqifiStreamingDevice` (`daqifi.devices`) |
| `DaqifiDeviceFactory` | `DaqifiDeviceFactory` (`daqifi.devices`) |
| `AnalogOutputChannel` / `AnalogInputChannel` / `DigitalIOChannel` | same names (`daqifi.channels`) |
| `SerialStreamTransport` / `TcpStreamTransport` | same names (`daqifi.transport`) |
| `SerialDeviceFinder` / `WiFiDeviceFinder` | same names (`daqifi.device_finders`) |
| `ProtobufMessageParser` | `ProtobufMessageParser` (`daqifi.communication`) |
| `ScpiMessageProducer.*Async` | `scpi_commands.*` factories (`daqifi._internal.scpi_commands`) |
| `IFirmwareUpdateService` / `FirmwareUpdateService` | `FirmwareUpdateService` Protocol / `DefaultFirmwareUpdateService` (`daqifi.firmware`) |
| `CsvExporter` / `ChannelDescriptor` / `SampleRow` / `ISampleSource` | same names (`daqifi.export`) |
| `SdCardOperationException` (+ subclasses) | same names (`daqifi.exceptions`) |

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
`scripts/portomatic_audit.py`) that reimplemented the equivalent
features.
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

## License

MIT License - Copyright (c) DAQiFi

## Support

For issues or questions:
- Create an issue in the [GitHub repository](https://github.com/daqifi/daqifi-nyquist-firmware/issues)
- Contact DAQiFi support
