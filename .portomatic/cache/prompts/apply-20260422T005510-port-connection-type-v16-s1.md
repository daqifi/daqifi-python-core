# portomatic apply prompt

**Plan:** `20260422T005510-port-connection-type-v16`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: Port ConnectionType enum to IntEnum, alongside DeviceType in _internal/comm.py
- classification: new-feature
- shard: Discovery
- upstream_ref: e8d84af:src/Daqifi.Core/Device/Discovery/IDeviceInfo.cs
- downstream_target: daqifi/_internal/comm.py

## Port conventions (project-wide)

- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

In addition: this project has a pytest surface at `tests/portomatic-generated/`. Write unit tests for the ported symbol at `tests/portomatic-generated/test_<slug>.py`; cover all branches and edge cases; run `python3 -m pytest tests/portomatic-generated/test_<slug>.py -q` and include the summary line in your report.

## Upstream file at this commit (full)

```csharp
using System.Net;

#nullable enable

namespace Daqifi.Core.Device.Discovery;

/// <summary>
/// Interface representing discovered device metadata.
/// </summary>
public interface IDeviceInfo
{
    /// <summary>
    /// Gets the device name/hostname.
    /// </summary>
    string Name { get; }

    /// <summary>
    /// Gets the device serial number.
    /// </summary>
    string SerialNumber { get; }

    /// <summary>
    /// Gets the firmware version.
    /// </summary>
    string FirmwareVersion { get; }

    /// <summary>
    /// Gets the IP address (for network devices).
    /// </summary>
    IPAddress? IPAddress { get; }

    /// <summary>
    /// Gets the MAC address (for network devices).
    /// </summary>
    string? MacAddress { get; }

    /// <summary>
    /// Gets the TCP port (for network devices).
    /// </summary>
    int? Port { get; }

    /// <summary>
    /// Gets the local interface address that discovered this device (for network devices).
    /// Used for binding TCP connections in multi-NIC scenarios.
    /// </summary>
    IPAddress? LocalInterfaceAddress { get; }

    /// <summary>
    /// Gets the device type.
    /// </summary>
    DeviceType Type { get; }

    /// <summary>
    /// Gets a value indicating whether the device is powered on.
    /// </summary>
    bool IsPowerOn { get; }

    /// <summary>
    /// Gets the connection type (WiFi, Serial, HID).
    /// </summary>
    ConnectionType ConnectionType { get; }

    /// <summary>
    /// Gets the port name (for serial devices).
    /// </summary>
    string? PortName { get; }

    /// <summary>
    /// Gets the device path (for HID devices).
    /// </summary>
    string? DevicePath { get; }
}

/// <summary>
/// Device type enumeration.
/// </summary>
public enum DeviceType
{
    /// <summary>
    /// Unknown device type.
    /// </summary>
    Unknown,

    /// <summary>
    /// Nyquist 1 device.
    /// </summary>
    Nyquist1,

    /// <summary>
    /// Nyquist 3 device.
    /// </summary>
    Nyquist3
}

/// <summary>
/// Connection type enumeration.
/// </summary>
public enum ConnectionType
{
    /// <summary>
    /// Unknown connection type.
    /// </summary>
    Unknown,

    /// <summary>
    /// WiFi/TCP connection.
    /// </summary>
    WiFi,

    /// <summary>
    /// Serial/USB connection.
    /// </summary>
    Serial,

    /// <summary>
    /// USB HID connection (bootloader mode).
    /// </summary>
    Hid
}

```

## ⚠ Multi-class upstream file (no `target_symbol` set)

The upstream file `src/Daqifi.Core/Device/Discovery/IDeviceInfo.cs` defines 3 top-level types: `IDeviceInfo` (not yet ported — separate plan step recommended), `DeviceType` (already ported in this target file at line 609), `ConnectionType` (not yet ported — separate plan step recommended).

The plan step has no explicit `target_symbol`. Identify which type the step's summary refers to and port ONLY that one. Setting `target_symbol` on future steps removes this ambiguity.

## Target rationale

Target file has 24 top-level declarations already — extending an established module. Match its existing structure.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 1: `# Module prelude (imports + top-level type definitions)`
      `Internal serial communication layer for DAQiFi Nyquist devices.`
      `This module provides low-level SCPI command interface over serial port.`
      `Not intended for direct use - use the public API instead.`
      `Ports the C# ``TransportStatusEventArgs`` (subclass of ``EventArgs``).`
      `Python uses plain callables instead of the .NET event pattern, so the`
-     line 661: `# All Nyquist variants currently share the same capability set`
          `nyquist_caps = dict(`
          `supports_streaming=True,`
          `has_sd_card=True,`
          `has_wifi=True,`
          `has_usb=True,`
-       line 840: `# Wait for device to stabilize`
            `time.sleep(0.1)`
-       line 842: `# Clear any pending data`
            `self._serial.reset_input_buffer()`
            `self._serial.reset_output_buffer()`
            `return True`
            `except (serial.SerialException, OSError) as e:`
            `return False`
-       line 877: `# Send command with CR terminator`
            `cmd_bytes = (command + "\r").encode('ascii')`
            `self._serial.write(cmd_bytes)`
            `self._serial.flush()`
-       line 882: `# Wait for device to process`
            `time.sleep(delay)`
-       line 885: `# Read all available data`
            `response = b""`
            `start_time = time.time()`
            `while (time.time() - start_time) < self.timeout:`
            `if self._serial.in_waiting > 0:`
            `chunk = self._serial.read(self._serial.in_waiting)`
-           line 893: `# Give a bit more time for any trailing data`
                `time.sleep(0.05)`
                `if self._serial.in_waiting == 0:`
                `break`
                `else:`
-           line 899: `# No data yet, wait a bit`
                `time.sleep(0.01)`
-       line 902: `# Decode and clean response`
            `response_str = response.decode('ascii', errors='ignore')`
-       line 905: `# Remove echoed command if present`
            `if command in response_str:`
            `response_str = response_str.replace(command, "")`
            `response_str = response_str.replace("DAQIFI>", "")`
-       line 912: `# Clean up whitespace and control characters`
            `response_str = response_str.strip()`
            `return response_str`
            `except (serial.SerialException, OSError) as e:`
            `raise RuntimeError(f"Serial communication error: {e}")`
            `def send_query(self, query: str, delay: float = 0.1) -> str:`
-     line 932: `# Remove any surrounding quotes from response`
          `response = response.strip('"').strip("'")`
          `return response`
          `def send_set(self, command: str, value, delay: float = 0.1) -> bool:`
-     line 948: `# Format value based on type`
          `if isinstance(value, str):`
          `cmd = f"{command} {value}"`
          `elif isinstance(value, bool):`
          `cmd = f"{command} {1 if value else 0}"`
          `elif isinstance(value, (int, float)):`
-     line 960: `# Check for errors`
          `error = self.send_query("SYST:ERR?", 0.1)`
          `return "No error" in error`
          `def clear_errors(self):`
          `max_errors = 10`
          `for _ in range(max_errors):`

**Top-level declarations** (existing module-level symbols — use as structural overview):

_Type definitions:_
- `class TransportStatusEventArgs`
- `class ConnectionRetryOptions`
- `class StreamTransport`
- `class IDataSample`
- `class DataSample`
- `class SampleReceivedEventArgs`
- `class OutboundMessage`
- `class ScpiMessage`
- `class IInboundMessage`
- `class GenericInboundMessage`
- `class TextInboundMessage`
- `class MessageProducer`
- `class MessageReceivedEventArgs`
- `class MessageConsumerErrorEventArgs`
- `class MessageConsumer`
- `class HidDeviceInfo`
- `class DeviceConnectionOptions`
- `class DeviceType`
- `class DeviceCapabilities`
- `class DeviceMetadata`
- `class PowerState`

_Implementations:_
- `class SerialInterface`

_Free functions:_
- `def detect_device_type_from_part_number`

_Type aliases:_
- `T (TypeVar/alias)`

**Nearest top-level declarations** (where new code typically lands):

- `class DeviceCapabilities`
- `class DeviceMetadata`
- `class PowerState`

_(full body elided — downstream target is 984 lines (over the 800-line aggressive-elide threshold) with 119 changed lines upstream; see the regions index + top-level declarations above for structural overview, and `daqifi/_internal/comm.py` for the full current file.)_

## Convention signals

Usage counts across the downstream tree. Pick the local idiom; zero-usage entries mean introducing this idiom would be a first.

- `@staticmethod`: 18 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 11 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 4 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 18 usages (e.g. `daqifi/enums.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 35 usages (e.g. `daqifi/communication.py`)

## Output requirements

- Edit the downstream file (or create it if missing) to implement the upstream change in idiomatic Python.
- Do NOT transliterate — port. Use Python idioms (dataclasses, properties, type hints, exceptions).
- Honour any active divergences listed above (don't 'fix' an intentional divergence).
- **Match the LOCAL section's convention**, not the whole file's most-common style. If the File regions index above shows multiple sections, identify which section your change belongs to and mimic THAT section's naming/formatting — different sections often use different conventions intentionally.
- For Python smoke checks, use `python3` (auto-detected for this project — venv preferred, else python3).
- After editing, run the verifier policy. Bound at N=3 reflection iterations.
- **On failure (verifier still failing after 3 attempts), STOP and report.** Do NOT call `AskUserQuestion`, do NOT file a sub-ticket yourself, do NOT keep iterating past N=3. Just include in your report:
  - The diff you applied (latest attempt)
  - The verifier output (last ~20 lines)
  - Your best guess at the failure class: `compile`, `behavioral_drift`, `missing_dep`, `flaky_test`, or `unknown`
  - The orchestrator marks the ticket `blocked`, surfaces the failure to the user, and decides skip/retry/abort.
- **DO NOT run `portomatic apply close` yourself** — the orchestrator that dispatched you owns the close step. Just report the diff you applied, whether tests passed, and any blockers. The orchestrator runs `apply close` after verifying.
