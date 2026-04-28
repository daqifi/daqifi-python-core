# portomatic apply prompt

**Plan:** `20260420T011927-port-channel-enums-v06`  ·  **Step:** `s2`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free

## Step

- summary: Port ChannelDirection enum (Input, Output, Unknown) -> Python enum
- classification: new-feature
- shard: Channel
- upstream_ref: e8d84af:src/Daqifi.Core/Channel/ChannelDirection.cs
- downstream_target: daqifi/_internal/scpi.py
- notes:
  - closed by portomatic apply close at 2026-04-20T01:22:13.094249+00:00

## Port conventions (project-wide)

- `IDisposable / Dispose()` → `close()` method, optionally pair with `__enter__`/`__exit__` for context-manager protocol
- `IEnumerable<T>` → `typing.Iterable[T]` (or `Iterator[T]` if you also need `next()` semantics)
- `event EventHandler<T>` → `Callable[[T], None]` callback list, or pub/sub helper if observers > 1
- `Task<T> (async)` → `Awaitable[T]` if the project uses asyncio; sync return otherwise (note divergence in shard)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
namespace Daqifi.Core.Channel;

/// <summary>
/// Represents the direction of data flow for a channel.
/// </summary>
public enum ChannelDirection
{
    /// <summary>
    /// Input channel (reads data from device).
    /// </summary>
    Input,

    /// <summary>
    /// Output channel (writes data to device).
    /// </summary>
    Output,

    /// <summary>
    /// Unknown or uninitialized direction.
    /// </summary>
    Unknown
}

```

## Target rationale

Target file already references `ChannelDirection` — extending in place. Look for the existing reference and append your change near it.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 24: `# System commands`
      `class System:`
      `IDN = "*IDN?"`
      `RST = "*RST"`
      `ERR = "SYST:ERR?"`
      `INFO = "SYST:INFO?"`
-   line 34: `# Power management`
        `POWER_STATE_QUERY = "SYST:POW:STAT?"`
        `POWER_STATE_SET = "SYST:POW:STAT"`
-   line 38: `# WiFi/LAN`
        `WIFI_SSID_QUERY = "SYST:COMM:LAN:SSID?"`
        `WIFI_SSID_SET = "SYST:COMM:LAN:SSID"`
        `WIFI_IP_QUERY = "SYST:COMM:LAN:ADDR?"`
        `WIFI_NETTYPE_QUERY = "SYST:COMM:LAN:NETTYPE?"  # Network type (1=STA client, 4=AP)`
        `WIFI_NETTYPE_SET = "SYST:COMM:LAN:NETTYPE"`
-   line 53: `# SD Card`
        `SD_STATUS_QUERY = "SYST:STOR:SD:STAT?"`
        `SD_ENABLE_SET = "SYST:STOR:SD:ENAble"`
        `SD_LOGGING_SET = "SYST:STOR:SD:LOGging"`
        `SD_LIST_QUERY = "SYST:STOR:SD:LISt?"`
        `SD_GET = "SYST:STOR:SD:GET"`
- line 73: `# Analog input (ADC) commands`
      `class AnalogInput:`
-   line 76: `# All channels enable/disable (bitmask)`
        `ENABLE_ALL_QUERY = "ENABle:VOLTage:DC?"`
        `ENABLE_ALL_SET = "ENABle:VOLTage:DC"`
-   line 80: `# Per-channel enable/disable`
        `ENABLE_CHANNEL_QUERY = "CONFigure:ADC:CHANnel?"`
        `ENABLE_CHANNEL_SET = "CONFigure:ADC:CHANnel"`
-   line 84: `# Channel range configuration`
        `RANGE_QUERY = "CONFigure:ADC:RANGe?"`
        `RANGE_SET = "CONFigure:ADC:RANGe"`
-   line 88: `# Sample rate`
        `SAMPLE_RATE_QUERY = "ACQuire:SAMPle:RATE?"`
        `SAMPLE_RATE_SET = "ACQuire:SAMPle:RATE"`
- line 93: `# Analog output (DAC) commands`
      `class AnalogOutput:`
-   line 96: `# Voltage control`
        `VOLTAGE_QUERY = "SOUR:VOLT:LEV?"`
        `VOLTAGE_SET = "SOUR:VOLT:LEV"`
-   line 100: `# Channel enable`
        `ENABLE_QUERY = "ENAB:SOUR:DC?"`
        `ENABLE_SET = "ENAB:SOUR:DC"`
-   line 104: `# Range configuration`
        `RANGE_QUERY = "CONF:DAC:RANG?"`
        `RANGE_SET = "CONF:DAC:RANG"`
-   line 108: `# Update outputs`
        `UPDATE = "CONF:DAC:UPDATE"`
-   line 111: `# Calibration`
        `CAL_SLOPE_SET = "CONF:DAC:chanCALM"`
        `CAL_OFFSET_SET = "CONF:DAC:chanCALB"`
        `CAL_SAVE = "CONF:DAC:SAVEcal"`
        `CAL_LOAD = "CONF:DAC:LOADcal"`
- line 118: `# Digital I/O commands`
      `class DigitalIO:`
-   line 121: `# Port-level commands (16-bit masks for all channels)`
        `PORT_DIRECTION_QUERY = "DIO:PORt:DIRection?"`
        `PORT_DIRECTION_SET = "DIO:PORt:DIRection"`
        `PORT_STATE_QUERY = "DIO:PORt:STATe?"`
        `PORT_STATE_SET = "DIO:PORt:STATe"`
        `PORT_ENABLE_QUERY = "DIO:PORt:ENAble?"`
- line 133: `# Streaming commands`
      `class Streaming:`
      `START = "SYSTem:StartStreamData"`
      `STOP = "SYSTem:StopStreamData"`
      `STATUS_QUERY = "SYSTem:StreamData?"`
      `FORMAT_SET = "SYSTem:STReam:FORmat"`
- line 147: `# Response parsing utilities`
      `def parse_bool(response: str) -> bool:`
      `response = response.strip()`
      `if response == "1":`
      `return True`
      `elif response == "0":`
-   line 178: `# Remove surrounding quotes if present`
        `if (response.startswith('"') and response.endswith('"')) or \`
        `(response.startswith("'") and response.endswith("'")):`
        `return response[1:-1]`
        `return response`
        `def parse_comma_separated(response: str) -> List[str]:`

**Top-level declarations** (existing module-level symbols — use as structural overview):

- `class ChannelType`
- `class ChannelDirection`
- `class System`
- `class AnalogInput`
- `class AnalogOutput`
- `class DigitalIO`
- `class Streaming`
- `def parse_bool`
- `def parse_int`
- `def parse_float`
- `def parse_string`
- `def parse_comma_separated`
- `def format_channel_command`
- `def is_error_response`

```python
"""
SCPI command definitions and utilities for Nyquist devices.

Provides command constants and response parsing functions.
"""

from enum import IntEnum
from typing import Optional, List


class ChannelType(IntEnum):
    """Type of a channel."""
    Digital = 0  # Digital channel (binary on/off)
    Analog = 1   # Analog channel (continuous value)


class ChannelDirection(IntEnum):
    """Direction of data flow for a channel."""
    Input = 0    # Input channel (reads data from device)
    Output = 1   # Output channel (writes data to device)
    Unknown = 2  # Unknown or uninitialized direction


# System commands
class System:
    """System-level SCPI commands"""
    IDN = "*IDN?"
    RST = "*RST"
    ERR = "SYST:ERR?"
    INFO = "SYST:INFO?"
    REBOOT = "SYST:REBOOT"
    FORCE_BOOTLOADER = "SYST:FORceBoot"

    # Power management
    POWER_STATE_QUERY = "SYST:POW:STAT?"
    POWER_STATE_SET = "SYST:POW:STAT"

    # WiFi/LAN
    WIFI_SSID_QUERY = "SYST:COMM:LAN:SSID?"
    WIFI_SSID_SET = "SYST:COMM:LAN:SSID"
    WIFI_IP_QUERY = "SYST:COMM:LAN:ADDR?"
    WIFI_NETTYPE_QUERY = "SYST:COMM:LAN:NETTYPE?"  # Network type (1=STA client, 4=AP)
    WIFI_NETTYPE_SET = "SYST:COMM:LAN:NETTYPE"
    WIFI_SIGNAL_QUERY = "SYST:COMM:LAN:SSIDSTR?"  # Signal strength
    WIFI_SECURITY_QUERY = "SYST:COMM:LAN:SECURITY?"  # Security mode (0=Open, 3=WPA)
    WIFI_SECURITY_SET = "SYST:COMM:LAN:SECURITY"
    WIFI_PASSKEY_SET = "SYST:COMM:LAN:PASS"  # Set password (WPA)
    WIFI_PASSKEY_CHECK = "SYST:COMM:LAN:PASSCHECK?"  # Verify password matches
    WIFI_APPLY = "SYST:COMM:LAN:APPLY"
    WIFI_SAVE = "SYST:COMM:LAN:SAVE"
    WIFI_LOAD = "SYST:COMM:LAN:LOAD"

    # SD Card
    SD_STATUS_QUERY = "SYST:STOR:SD:STAT?"
    SD_ENABLE_SET = "SYST:STOR:SD:ENAble"
    SD_LOGGING_SET = "SYST:STOR:SD:LOGging"
    SD_LIST_QUERY = "SYST:STOR:SD:LISt?"
    SD_GET = "SYST:STOR:SD:GET"
    SD_DELETE = "SYST:STOR:SD:DELete"
    SD_FORMAT = "SYST:STOR:SD:FORmat"
    SD_BENCHMARK = "SYST:STOR:SD:BENCHmark"
    SD_BENCHMARK_QUERY = "SYST:STOR:SD:BENCHmark?"

    @staticmethod
    def set_sd_logging_filename(filename: str) -> str:
        """Build command to set the SD card logging file name.

        The file is created if it doesn't exist, or appended to if it does.
        """
        return f'{System.SD_LOGGING_SET} "{filename}"'


# Analog input (ADC) commands
class AnalogInput:
    """ADC SCPI commands"""
    # All channels enable/disable (bitmask)
    ENABLE_ALL_QUERY = "ENABle:VOLTage:DC?"
    ENABLE_ALL_SET = "ENABle:VOLTage:DC"

    # Per-channel enable/disable
    ENABLE_CHANNEL_QUERY = "CONFigure:ADC:CHANnel?"
    ENABLE_CHANNEL_SET = "CONFigure:ADC:CHANnel"

    # Channel range configuration
    RANGE_QUERY = "CONFigure:ADC:RANGe?"
    RANGE_SET = "CONFigure:ADC:RANGe"

    # Sample rate
    SAMPLE_RATE_QUERY = "ACQuire:SAMPle:RATE?"
    SAMPLE_RATE_SET = "ACQuire:SAMPle:RATE"


# Analog output (DAC) commands
class AnalogOutput:
    """DAC SCPI commands (NQ3 only)"""
    # Voltage control
    VOLTAGE_QUERY = "SOUR:VOLT:LEV?"
    VOLTAGE_SET = "SOUR:VOLT:LEV"

    # Channel enable
    ENABLE_QUERY = "ENAB:SOUR:DC?"
    ENABLE_SET = "ENAB:SOUR:DC"

    # Range configuration
    RANGE_QUERY = "CONF:DAC:RANG?"
    RANGE_SET = "CONF:DAC:RANG"

    # Update outputs
    UPDATE = "CONF:DAC:UPDATE"

    # Calibration
    CAL_SLOPE_SET = "CONF:DAC:chanCALM"
    CAL_OFFSET_SET = "CONF:DAC:chanCALB"
    CAL_SAVE = "CONF:DAC:SAVEcal"
    CAL_LOAD = "CONF:DAC:LOADcal"


# Digital I/O commands
class DigitalIO:
    """DIO SCPI commands"""
    # Port-level commands (16-bit masks for all channels)
    PORT_DIRECTION_QUERY = "DIO:PORt:DIRection?"
    PORT_DIRECTION_SET = "DIO:PORt:DIRection"
    PORT_STATE_QUERY = "DIO:PORt:STATe?"
    PORT_STATE_SET = "DIO:PORt:STATe"
    PORT_ENABLE_QUERY = "DIO:PORt:ENAble?"
    PORT_ENABLE_SET = "DIO:PORt:ENAble"

    # NOTE: Per-channel commands do not exist in firmware
    # Use port-level commands with bit masking for individual channels


# Streaming commands
class Streaming:
    """Data streaming SCPI commands"""
    START = "SYSTem:StartStreamData"
    STOP = "SYSTem:StopStreamData"
    STATUS_QUERY = "SYSTem:StreamData?"
    FORMAT_SET = "SYSTem:STReam:FORmat"
    FORMAT_QUERY = "SYSTem:STReam:FORmat?"
    INTERFACE_SET = "SYSTem:STReam:INTerface"  # 0=USB, 1=WiFi, 2=SD, 3=All
    INTERFACE_QUERY = "SYSTem:STReam:INTerface?"
    STATS_QUERY = "SYSTem:STReam:Stats?"
    STATS_CLEAR = "SYSTem:STReam:ClearStats"


# Response parsing utilities
def parse_bool(response: str) -> bool:
    """Parse SCPI boolean response (0/1)"""
    response = response.strip()
    if response == "1":
        return True
    elif response == "0":
        return False
    else:
        raise ValueError(f"Invalid boolean response: {response}")


def parse_int(response: str) -> int:
    """Parse SCPI integer response"""
    try:
        return int(response.strip())
    except ValueError:
        raise ValueError(f"Invalid integer response: {response}")


def parse_float(response: str) -> float:
    """Parse SCPI float response"""
    try:
        return float(response.strip())
    except ValueError:
        raise ValueError(f"Invalid float response: {response}")


def parse_string(response: str) -> str:
    """Parse SCPI string response (removes quotes)"""
    response = response.strip()
    # Remove surrounding quotes if present
    if (response.startswith('"') and response.endswith('"')) or \
       (response.startswith("'") and response.endswith("'")):
        return response[1:-1]
    return response


def parse_comma_separated(response: str) -> List[str]:
    """Parse comma-separated response"""
    return [item.strip() for item in response.split(',') if item.strip()]


def format_channel_command(base_command: str, channel: int) -> str:
    """Format command with channel number"""
    if "{ch}" in base_command:
        return base_command.format(ch=channel)
    return base_command


def is_error_response(response: str) -> bool:
    """Check if response indicates an error"""
    response_upper = response.upper()
    return ("**ERROR" in response_upper or
            "ERROR" in response_upper and "NO ERROR" not in response_upper)

```

## Convention signals

Usage counts across the downstream tree. Pick the local idiom; zero-usage entries mean introducing this idiom would be a first.

- `@staticmethod`: 5 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 1 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 2 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.

## Map shard `Channel` — relevant entries


### Architectural context (no exact file match)

_Nearest file mappings in this shard — read these BEFORE deciding whether to create a downstream file:_

- `src/Daqifi.Core/Channel/AnalogChannel.cs` → `daqifi/channels.py` (divergent)
  > C# AnalogChannel is a self-contained data model with local
  > scaling/calibration math (GetScaledValue computes (raw / resolution
  > * port_range * cal_m + cal_b) * internal_scale_m client-side).
  > Python AnalogInputChannel is a thin SCPI wrapper with no scaling
  > math — the device computes scaled values and the Python class
  > delegates everything via send_query/send_set. There is no Python
  > class with field-level parity to C# AnalogChannel and no plan to
  > add one (the Python port is intentionally device-delegated).
- `src/Daqifi.Core/Channel/IAnalogChannel.cs` → `(none)` (dropped)
  > Python uses duck typing; no separate interface file.
- `src/Daqifi.Core/Channel/DigitalChannel.cs` → `daqifi/channels.py` (divergent)
  > Same as AnalogChannel — Python DigitalIOChannel is SCPI-delegated.

**Active divergences in this area:**
- `DIV-001` (behavioral): Python port architecturally chooses to delegate scaling/calibration
to the device firmware rather than recomputing client-side. This
means C# tests like GetScaledValue_*, Constructor_*, Properties_*,
ToString_ReturnsChannelName have NO Python equivalent and should
not be ported. New C# AnalogChannel features that touch scaling
math should also be classified as `divergent` on /portomatic-check
until the Python architecture changes.


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
