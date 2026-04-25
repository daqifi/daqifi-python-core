# daqifi-core → Python port notes

Companion document for the major port batch landed on branch
`portomatic-port-try-1` (commits `c6855b6` → `88a7b49`,
2026-04-22).

## Summary

Ported the full public surface of the C# `Daqifi.Core` library to
Python, taking type-by-type coverage from **79.3% → 95.5%** across
seven batches. Adds **7 new Python modules** (≈3,400 LOC) and
**~150 new tests**. The user-facing `daqifi.device.NyquistDevice` API
is unchanged.

## New modules

These mirror the daqifi-core C# layout. They are lower-level than
`NyquistDevice` (which is a high-level convenience API for end users);
think of them as the building blocks the C# library exposes for
custom transports + protocol-driven device control.

| Python module | Mirrors C# | Purpose |
| --- | --- | --- |
| `daqifi/_internal/scpi_commands.py` | `Communication/Producers/ScpiMessageProducer.cs` | 44 SCPI command factory functions (`reboot_device()`, `set_network_wifi_ssid(...)`, ...) |
| `daqifi/_internal/hid_enumerator.py` | `Communication/Transport/HidLibraryDeviceEnumerator.cs` | HID enumerator with injectable platform layer (lazy hidapi import) |
| `daqifi/transport.py` | `Communication/Transport/{Tcp,Udp,Serial,HidLibrary}*Transport.cs` | 4 concrete transports satisfying the `StreamTransport` / `UdpTransport` / `HidTransport` Protocols |
| `daqifi/communication.py` (extended) | `Communication/Consumers/*` | Adds `ProtobufMessageParser`, `CompositeMessageParser`, `StreamMessageConsumer`, `ObjectInboundMessage` |
| `daqifi/devices.py` | `Device/{DaqifiDevice,DaqifiStreamingDevice,DaqifiDeviceFactory}.cs` | Lower-level device classes wired around a transport + protocol-handler |
| `daqifi/firmware/github_download_service.py` | `Firmware/GitHubFirmwareDownloadService.cs` | GitHub-backed concrete impl of the existing `FirmwareDownloadService` Protocol |
| `daqifi/device_finders.py` | `Device/Discovery/{Hid,Serial,WiFi}DeviceFinder.cs` | Discovery finders (HID enumeration, serial probing, UDP broadcast) |

Plus:

- `daqifi/_internal/comm.py` — added `Device`, `StreamingDevice`,
  `HidDeviceEnumerator`, `HidTransport`, `UdpTransport`,
  `MessageParser`, `DeviceStatusEventArgs` Protocols + dataclass
- `daqifi/_internal/scpi.py` — added `DeviceState` IntEnum

## Layer relationship

```
+-------------------------------------------------------------+
|  daqifi.device.NyquistDevice         (existing user API)    |
|     wraps SerialInterface, exposes channels/sd/etc          |
+-------------------------------------------------------------+
|  daqifi.devices.DaqifiStreamingDevice  (new, port of C#)    |
|     wraps a transport + protobuf consumer + protocol        |
|     handler; gives you the same surface the C# library      |
|     gives C# callers                                        |
+-------------------------------------------------------------+
|  daqifi.transport.{Tcp,Udp,Serial,HidLibrary}*Transport     |
|  daqifi._internal.scpi_commands.* (factories)               |
|  daqifi.communication.{Stream,Composite,Protobuf}*          |
+-------------------------------------------------------------+
```

`NyquistDevice` and `DaqifiStreamingDevice` are parallel APIs at
different abstraction levels — both work, both have tests. There's
some overlap in SCPI command vocabulary today (legacy
`_internal/scpi.py:System` constants vs new
`_internal/scpi_commands.py` functions); see
[issue #12](https://github.com/daqifi/daqifi-python-core/issues/12)
for the consolidation plan.

## Test counts

```
pytest tests/portomatic-generated -q
# 602 passed, 1 xfailed
```

Hardware-coupled probe loops (real serial enumeration, real UDP
broadcast, real HID open) are intentionally exercised under
`tests/integration/` only — those run against an attached DAQiFi.

## Hardware validation TODO

The following code paths are mocked in unit tests but need
on-hardware exercise before being trusted in production:

- `daqifi.transport.SerialStreamTransport.connect()` against a real
  serial port at 9600/115200 baud
- `daqifi.transport.TcpStreamTransport.connect()` against a streaming
  device on port 9760
- `daqifi.transport.HidLibraryTransport.connect()` against a device
  in bootloader mode (VID 0x04D8, PID 0x003C)
- `daqifi.device_finders.SerialDeviceFinder.discover()` — full probe
  loop including SCPI init sequence and protobuf detection
- `daqifi.device_finders.WiFiDeviceFinder.discover()` — UDP broadcast
  + response parsing on a real network
- `daqifi.devices.DaqifiStreamingDevice.initialize()` + streaming
  start/stop against a real device

Plan: add scenarios under `tests/integration/` for each of these and
gate on `DAQIFI_DEVICE_REQUIRED=1` in CI when a board is attached.

## Tooling

The `tools/` directory ships three port-helper micro-tools:

- `tools/find_upstream.sh SYMBOL [...]` — locate a C# class/interface
  in `../daqifi-core/`
- `tools/find_downstream.sh SYMBOL [...]` — locate a Python
  class/Protocol/function in this repo
- `tools/coverage_status.py SYMBOL [...]` — report portomatic
  coverage status for one or more upstream symbol names
- `tools/coverage_status.py --suspicious` — list ported entries the
  classifier is likely wrong about (low-confidence overlaps,
  kind-mismatches)

`tools/portomatic-improvement-log.md` is the running list of
classifier + scaffold improvements found during the port. Six items
filed; see
[portomatic#140](https://github.com/cptkoolbeenz/portomatic/issues/140),
[#141](https://github.com/cptkoolbeenz/portomatic/issues/141),
[#142](https://github.com/cptkoolbeenz/portomatic/issues/142),
[#143](https://github.com/cptkoolbeenz/portomatic/issues/143).

## USB CDC buffer sizing

`SerialStreamTransport` requests a 64 KiB OS-level RX/TX buffer via
pyserial's `set_buffer_size()` after open, and `StreamMessageConsumer`
reads in matching 64 KiB chunks so a single syscall drains the OS
buffer when full. The defaults are exposed as constructor parameters
(`os_rx_buffer_bytes`, `os_tx_buffer_bytes`, `buffer_size`) for
callers that want to tune.

**Why it matters.** The DAQiFi firmware's USB CDC writer drops bytes
if the host doesn't read within roughly 1 ms (per
`daqifi-python-test-suite/test_harness.py:106`'s `FastReader` notes).
The OS-level buffer is the cushion that absorbs scheduling jitter
between the device emitting a burst and the consumer thread waking
to drain it. A small driver buffer + slow read = drops.

**Platform behavior.**

- **Windows.** `pyserial`'s `set_buffer_size()` calls Win32
  `SetupComm()`, which the `usbser.sys` driver honors. Larger
  buffers are real.
- **Linux / WSL2.** `pyserial` on Linux raises `NotImplementedError`
  for `set_buffer_size()`; the call is caught and ignored. The
  `cdc_acm` kernel driver allocates URBs based on the device's USB
  endpoint MaxPacketSize and a fixed `NR_URBS` (default 16). Tuning
  requires kernel-side changes — see follow-up ticket for the
  benchmark-driven recommendation.
- **macOS.** Similar to Linux — pyserial's call is a no-op.

## Known coverage-classifier quirks (cosmetic, not real port gaps)

- `ScpiMessageProducer` shows as **missing** in the coverage report —
  it is in fact ported as `_internal/scpi_commands.py` (44/44
  functions, full parametrize tests). The classifier's
  static-class → module-of-functions strategy doesn't recognize this
  case; tracked in
  [portomatic#140](https://github.com/cptkoolbeenz/portomatic/issues/140).
- A few false-positive token-overlap matches (`IDevice` →
  `DeviceType`, `IMessageParser` → `IInboundMessage`, etc.) are
  resolved when the real Python ports land — they're cosmetic in the
  meantime. Tracked in
  [portomatic#141](https://github.com/cptkoolbeenz/portomatic/issues/141).
