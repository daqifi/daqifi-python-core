# portomatic apply prompt

**Plan:** `20260422T005341-port-stream-interface-v16`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: Port StreamInterface enum to Python IntEnum, mirroring ChannelType/ChannelDirection convention
- classification: new-feature
- shard: Communication
- upstream_ref: e8d84af:src/Daqifi.Core/Communication/StreamInterface.cs
- downstream_target: daqifi/_internal/scpi.py

## Port conventions (project-wide)

- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Stream` → `typing.IO[bytes]` (or `BinaryIO`) — file-like objects with read/write

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

In addition: this project has a pytest surface at `tests/portomatic-generated/`. Write unit tests for the ported symbol at `tests/portomatic-generated/test_<slug>.py`; cover all branches and edge cases; run `python3 -m pytest tests/portomatic-generated/test_<slug>.py -q` and include the summary line in your report.

## Upstream file at this commit (full)

```csharp
namespace Daqifi.Core.Communication;

/// <summary>
/// Specifies the target interface for data streaming.
/// </summary>
/// <remarks>
/// Controls where the device sends streaming data. Values correspond to firmware
/// SCPI command <c>SYSTem:STReam:INTerface</c> parameter values.
/// </remarks>
public enum StreamInterface
{
    /// <summary>
    /// Stream data over USB.
    /// </summary>
    Usb = 0,

    /// <summary>
    /// Stream data over WiFi.
    /// </summary>
    WiFi = 1,

    /// <summary>
    /// Stream data to the SD card.
    /// </summary>
    SdCard = 2,

    /// <summary>
    /// Stream data to all interfaces simultaneously.
    /// </summary>
    All = 3
}

```

## Target rationale

Target file has 15 top-level declarations already — extending an established module. Match its existing structure.

**Adjacent divergences in this subtree:**
- Active divergence **DIV-002** covers `src/Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs` (same subtree): _C# represents commands as Producer/Message objects (with Data + Format_ — check before porting.
- Active divergence **DIV-003** covers `src/Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs` (same subtree): _C# uses SCPI long form ("SYSTem:STORage:..."); Python uses short form_ — check before porting.

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

- `@staticmethod`: 18 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 11 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 4 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 16 usages (e.g. `daqifi/enums.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 35 usages (e.g. `daqifi/communication.py`)

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
