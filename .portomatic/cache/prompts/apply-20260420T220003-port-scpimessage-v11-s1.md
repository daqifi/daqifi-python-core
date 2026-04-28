# portomatic apply prompt

**Plan:** `20260420T220003-port-scpimessage-v11`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.11 test 3: Re-port ScpiMessage — minimal class, control case for prompt cleanliness
- classification: new-feature
- shard: Communication
- upstream_ref: e8d84af:src/Daqifi.Core/Communication/Messages/ScpiMessage.cs
- downstream_target: daqifi/_internal/scpi.py

## Port conventions (project-wide)

- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.

## .NET helper idioms

Common BCL helpers that recur across nearly every C#→Python port. Use the listed equivalent rather than re-deriving the translation per call site:

| C# | Python equivalent |
|---|---|
| `string.IsNullOrWhiteSpace(s)` | `not s or not s.strip()` |
| `string.IsNullOrEmpty(s)` | `not s` (works for None and empty string) |
| `string.Format("{0}", x)` | `f"{x}"` (f-string) — fall back to `.format()` only when the format string is dynamic |
| `string.Join(",", items)` | `",".join(items)` when items are strings; `",".join(str(x) for x in items)` when not — Python `str.join` raises `TypeError` on non-string elements while .NET `string.Join` stringifies them. Note the order flip — separator first in C#, last in Python. |

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
using System.Text;

namespace Daqifi.Core.Communication.Messages;

/// <summary>
/// Represents a Standard Commands for Programmable Instruments (SCPI) message
/// to be sent to the device (Outbound).
/// Implements IOutboundMessage.
/// </summary>
/// <param name="command">The SCPI command string.</param>
public class ScpiMessage(string command) : IOutboundMessage<string>
{
    /// <summary>
    /// Gets or sets the data associated with the message, which is the SCPI command string.
    /// </summary>
    public string Data { get; set; } = command;

    /// <summary>
    /// Converts the SCPI command string to a byte array suitable for transmission,
    /// appending the required carriage return and newline characters.
    /// </summary>
    /// <returns>A byte array representing the SCPI message.</returns>
    public byte[] GetBytes()
    {
        return Encoding.ASCII.GetBytes(Data + "\r\n");
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `IOutboundMessage` **[base contract]** (from `src/Daqifi.Core/Communication/Messages/IOutboundMessage.cs`)

```csharp
public interface IOutboundMessage<T>
{
    /// <summary>
    /// Gets or sets the payload data to be sent.
    /// </summary>
    T Data { get; set;}

    /// <summary>
    /// Converts the message data into a byte array suitable for transmission.
    /// </summary>
    /// <returns>A byte array representing the message.</returns>
    byte[] GetBytes();
} 
```

- **Existing downstream implementations** (follow this pattern):
  - `class OutboundMessage` at `daqifi/_internal/comm.py:254` _(by name match)_
## Target rationale

Target file has 15 top-level declarations already — extending an established module. Match its existing structure.

**Sibling files mapped non-trivially in this subtree:**
- Sibling file `src/Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs` → `daqifi/_internal/scpi.py` (divergent) — consider whether this port belongs alongside that sibling.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 1: `# Module prelude (imports + top-level type definitions)`
      `SCPI command definitions and utilities for Nyquist devices.`
      `Provides command constants and response parsing functions.`
      `Digital = 0  # Digital channel (binary on/off)`
      `Analog = 1   # Analog channel (continuous value)`
      `class ChannelDirection(IntEnum):`
- line 42: `# System commands`
      `class System:`
      `IDN = "*IDN?"`
      `RST = "*RST"`
      `ERR = "SYST:ERR?"`
      `INFO = "SYST:INFO?"`
-   line 52: `# Power management`
        `POWER_STATE_QUERY = "SYST:POW:STAT?"`
        `POWER_STATE_SET = "SYST:POW:STAT"`
-   line 56: `# WiFi/LAN`
        `WIFI_SSID_QUERY = "SYST:COMM:LAN:SSID?"`
        `WIFI_SSID_SET = "SYST:COMM:LAN:SSID"`
        `WIFI_IP_QUERY = "SYST:COMM:LAN:ADDR?"`
        `WIFI_NETTYPE_QUERY = "SYST:COMM:LAN:NETTYPE?"  # Network type (1=STA client, 4=AP)`
        `WIFI_NETTYPE_SET = "SYST:COMM:LAN:NETTYPE"`
-   line 71: `# SD Card`
        `SD_STATUS_QUERY = "SYST:STOR:SD:STAT?"`
        `SD_ENABLE_SET = "SYST:STOR:SD:ENAble"`
        `SD_LOGGING_SET = "SYST:STOR:SD:LOGging"`
        `SD_LOGGING_QUERY = "SYST:STOR:SD:LOGging?"`
        `SD_LIST_QUERY = "SYST:STOR:SD:LISt?"`
- line 122: `# Analog input (ADC) commands`
      `class AnalogInput:`
-   line 125: `# All channels enable/disable (bitmask)`
        `ENABLE_ALL_QUERY = "ENABle:VOLTage:DC?"`
        `ENABLE_ALL_SET = "ENABle:VOLTage:DC"`
-   line 129: `# Per-channel enable/disable`
        `ENABLE_CHANNEL_QUERY = "CONFigure:ADC:CHANnel?"`
        `ENABLE_CHANNEL_SET = "CONFigure:ADC:CHANnel"`
-   line 133: `# Channel range configuration`
        `RANGE_QUERY = "CONFigure:ADC:RANGe?"`
        `RANGE_SET = "CONFigure:ADC:RANGe"`
-   line 137: `# Sample rate`
        `SAMPLE_RATE_QUERY = "ACQuire:SAMPle:RATE?"`
        `SAMPLE_RATE_SET = "ACQuire:SAMPle:RATE"`
- line 142: `# Analog output (DAC) commands`
      `class AnalogOutput:`
-   line 145: `# Voltage control`
        `VOLTAGE_QUERY = "SOUR:VOLT:LEV?"`
        `VOLTAGE_SET = "SOUR:VOLT:LEV"`
-   line 149: `# Channel enable`
        `ENABLE_QUERY = "ENAB:SOUR:DC?"`
        `ENABLE_SET = "ENAB:SOUR:DC"`
-   line 153: `# Range configuration`
        `RANGE_QUERY = "CONF:DAC:RANG?"`
        `RANGE_SET = "CONF:DAC:RANG"`
-   line 157: `# Update outputs`
        `UPDATE = "CONF:DAC:UPDATE"`
-   line 160: `# Calibration`
        `CAL_SLOPE_SET = "CONF:DAC:chanCALM"`
        `CAL_OFFSET_SET = "CONF:DAC:chanCALB"`
        `CAL_SAVE = "CONF:DAC:SAVEcal"`
        `CAL_LOAD = "CONF:DAC:LOADcal"`
- line 167: `# Digital I/O commands`
      `class DigitalIO:`
-   line 170: `# Port-level commands (16-bit masks for all channels)`
        `PORT_DIRECTION_QUERY = "DIO:PORt:DIRection?"`
        `PORT_DIRECTION_SET = "DIO:PORt:DIRection"`
        `PORT_STATE_QUERY = "DIO:PORt:STATe?"`
        `PORT_STATE_SET = "DIO:PORt:STATe"`
        `PORT_ENABLE_QUERY = "DIO:PORt:ENAble?"`
- line 182: `# Streaming commands`
      `class Streaming:`
      `START = "SYSTem:StartStreamData"`
      `STOP = "SYSTem:StopStreamData"`
      `STATUS_QUERY = "SYSTem:StreamData?"`
      `FORMAT_SET = "SYSTem:STReam:FORmat"`
- line 196: `# Response parsing utilities`
      `def parse_bool(response: str) -> bool:`
      `response = response.strip()`
      `if response == "1":`
      `return True`
      `elif response == "0":`
-   line 227: `# Remove surrounding quotes if present`
        `if (response.startswith('"') and response.endswith('"')) or \`
        `(response.startswith("'") and response.endswith("'")):`
        `return response[1:-1]`
        `return response`
        `def parse_comma_separated(response: str) -> List[str]:`

**Top-level declarations** (existing module-level symbols — use as structural overview):

_Type definitions:_
- `class ChannelType`
- `class ChannelDirection`
- `class ConnectionStatus`

_Implementations:_
- `class System`
- `class AnalogInput`
- `class AnalogOutput`
- `class DigitalIO`
- `class Streaming`

_Free functions:_
- `def parse_bool`
- `def parse_int`
- `def parse_float`
- `def parse_string`
- `def parse_comma_separated`
- `def format_channel_command`
- `def is_error_response`

**Nearest top-level declarations** (where new code typically lands):

- `def parse_comma_separated`
- `def format_channel_command`
- `def is_error_response`

_(full body elided — downstream target is 250 lines and the File regions index above already carries the structural overview; see the regions index + top-level declarations above for structural overview, and `daqifi/_internal/scpi.py` for the full current file.)_

## Convention signals

Usage counts across the downstream tree. Pick the local idiom; zero-usage entries mean introducing this idiom would be a first.

- `@staticmethod`: 10 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 10 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 4 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 9 usages (e.g. `daqifi/_internal/comm.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 18 usages (e.g. `daqifi/discovery.py`)

## Map shard `Communication` — relevant entries


### Architectural context (no exact file match)

_Nearest file mappings in this shard — read these BEFORE deciding whether to create a downstream file:_

- `src/Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs` → `daqifi/_internal/scpi.py` (divergent)

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
