# portomatic apply prompt

**Plan:** `20260420T044036-port-connstatus-v08`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: #18: Port ConnectionStatus enum (6 values)
- classification: new-feature
- shard: Device
- upstream_ref: e8d84af:src/Daqifi.Core/Device/ConnectionStatus.cs
- downstream_target: daqifi/_internal/scpi.py

## Port conventions (project-wide)

- `IDisposable / Dispose()` → `close()` method, optionally pair with `__enter__`/`__exit__` for context-manager protocol
- `IEnumerable<T>` → `typing.Iterable[T]` (or `Iterator[T]` if you also need `next()` semantics)
- `event EventHandler<T>` → Single event: `Callable[[T], None]` callback slot OR `add_<event>`/`remove_<event>` if subscribers > 1. Multiple events on same interface: prefix add/remove with the event name (e.g. `add_message_received`, `remove_message_received`) and keep callback lists per-event. Mirror this pattern across all events on the same Protocol/class for consistency.
- `Task<T> (async)` → `Awaitable[T]` if the project uses asyncio; sync return otherwise (note divergence in shard)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Stream` → `typing.IO[bytes]` (or `BinaryIO`) — file-like objects with read/write
- `TimeSpan` → `float` (seconds) or `datetime.timedelta` if duration arithmetic is needed
- `DateTime / DateTimeOffset` → `datetime.datetime` (use `datetime.now(timezone.utc)` for UTC equivalent of `DateTime.UtcNow`)
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `InvalidOperationException` → `RuntimeError` (or a domain-specific exception when the project defines one)
- `NotImplementedException / NotSupportedException` → `NotImplementedError`
- `EventArgs subclass` → `@dataclass` carrying the event payload (frozen if the event payload is read-only)
- `Guid` → `uuid.UUID`
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
namespace Daqifi.Core.Device
{
    /// <summary>
    /// Represents the connection status of a device.
    /// </summary>
    public enum ConnectionStatus
    {
        /// <summary>
        /// The device is disconnected.
        /// </summary>
        Disconnected,

        /// <summary>
        /// The device is in the process of connecting.
        /// </summary>
        Connecting,

        /// <summary>
        /// The device is connected.
        /// </summary>
        Connected,

        /// <summary>
        /// The device connection has been lost.
        /// </summary>
        Lost,

        /// <summary>
        /// The device is retrying connection after a failure.
        /// </summary>
        Retrying,

        /// <summary>
        /// The device connection failed after all retry attempts.
        /// </summary>
        Failed
    }
} 
```

## Target rationale

Target file has 14 top-level declarations already — extending an established module. Match its existing structure.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 1: `# Module prelude (imports + top-level type definitions)`
      `SCPI command definitions and utilities for Nyquist devices.`
      `Provides command constants and response parsing functions.`
      `Digital = 0  # Digital channel (binary on/off)`
      `Analog = 1   # Analog channel (continuous value)`
      `class ChannelDirection(IntEnum):`
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
        `SD_LOGGING_QUERY = "SYST:STOR:SD:LOGging?"`
        `SD_LIST_QUERY = "SYST:STOR:SD:LISt?"`
- line 104: `# Analog input (ADC) commands`
      `class AnalogInput:`
-   line 107: `# All channels enable/disable (bitmask)`
        `ENABLE_ALL_QUERY = "ENABle:VOLTage:DC?"`
        `ENABLE_ALL_SET = "ENABle:VOLTage:DC"`
-   line 111: `# Per-channel enable/disable`
        `ENABLE_CHANNEL_QUERY = "CONFigure:ADC:CHANnel?"`
        `ENABLE_CHANNEL_SET = "CONFigure:ADC:CHANnel"`
-   line 115: `# Channel range configuration`
        `RANGE_QUERY = "CONFigure:ADC:RANGe?"`
        `RANGE_SET = "CONFigure:ADC:RANGe"`
-   line 119: `# Sample rate`
        `SAMPLE_RATE_QUERY = "ACQuire:SAMPle:RATE?"`
        `SAMPLE_RATE_SET = "ACQuire:SAMPle:RATE"`
- line 124: `# Analog output (DAC) commands`
      `class AnalogOutput:`
-   line 127: `# Voltage control`
        `VOLTAGE_QUERY = "SOUR:VOLT:LEV?"`
        `VOLTAGE_SET = "SOUR:VOLT:LEV"`
-   line 131: `# Channel enable`
        `ENABLE_QUERY = "ENAB:SOUR:DC?"`
        `ENABLE_SET = "ENAB:SOUR:DC"`
-   line 135: `# Range configuration`
        `RANGE_QUERY = "CONF:DAC:RANG?"`
        `RANGE_SET = "CONF:DAC:RANG"`
-   line 139: `# Update outputs`
        `UPDATE = "CONF:DAC:UPDATE"`
-   line 142: `# Calibration`
        `CAL_SLOPE_SET = "CONF:DAC:chanCALM"`
        `CAL_OFFSET_SET = "CONF:DAC:chanCALB"`
        `CAL_SAVE = "CONF:DAC:SAVEcal"`
        `CAL_LOAD = "CONF:DAC:LOADcal"`
- line 149: `# Digital I/O commands`
      `class DigitalIO:`
-   line 152: `# Port-level commands (16-bit masks for all channels)`
        `PORT_DIRECTION_QUERY = "DIO:PORt:DIRection?"`
        `PORT_DIRECTION_SET = "DIO:PORt:DIRection"`
        `PORT_STATE_QUERY = "DIO:PORt:STATe?"`
        `PORT_STATE_SET = "DIO:PORt:STATe"`
        `PORT_ENABLE_QUERY = "DIO:PORt:ENAble?"`
- line 164: `# Streaming commands`
      `class Streaming:`
      `START = "SYSTem:StartStreamData"`
      `STOP = "SYSTem:StopStreamData"`
      `STATUS_QUERY = "SYSTem:StreamData?"`
      `FORMAT_SET = "SYSTem:STReam:FORmat"`
- line 178: `# Response parsing utilities`
      `def parse_bool(response: str) -> bool:`
      `response = response.strip()`
      `if response == "1":`
      `return True`
      `elif response == "0":`
-   line 209: `# Remove surrounding quotes if present`
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
    SD_LOGGING_QUERY = "SYST:STOR:SD:LOGging?"
    SD_LIST_QUERY = "SYST:STOR:SD:LISt?"
    SD_GET = "SYST:STOR:SD:GET"
    SD_DELETE = "SYST:STOR:SD:DELete"
    SD_FORMAT = "SYST:STOR:SD:FORmat"
    SD_BENCHMARK = "SYST:STOR:SD:BENCHmark"
    SD_BENCHMARK_QUERY = "SYST:STOR:SD:BENCHmark?"

    @staticmethod
    def get_sd_logging_state() -> str:
        """Build query to get the current SD card logging state."""
        return System.SD_LOGGING_QUERY

    @staticmethod
    def enable_storage_sd() -> str:
        """Build command to enable SD card logging.

        Note: LAN must be disabled first to enable SD card logging.
        """
        return f"{System.SD_ENABLE_SET} 1"

    @staticmethod
    def get_sd_file_list() -> str:
        """Build query to get the list of files on the SD card."""
        return System.SD_LIST_QUERY

    @staticmethod
    def set_sd_logging_filename(filename: str) -> str:
        """Build command to set the SD card logging file name.

        The file is created if it doesn't exist, or appended to if it does.
        """
        return f'{System.SD_LOGGING_SET} "{filename}"'

    @staticmethod
    def get_sd_file(filename: str) -> str:
        """Build command to retrieve a specific file from the SD card."""
        return f'{System.SD_GET} "{filename}"'

    @staticmethod
    def delete_sd_file(filename: str) -> str:
        """Build command to delete a file from the SD card."""
        if not filename or not filename.strip():
            raise ValueError("Filename cannot be null or empty.")
        return f'{System.SD_DELETE} "{filename}"'


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

- `@staticmethod`: 10 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 4 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 3 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 5 usages (e.g. `daqifi/_internal/comm.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 8 usages (e.g. `daqifi/discovery.py`)

## Research hits (cached docs containing the shard name)

- `scpi_protocol/ieee-488-2-scpi-command-parsing-rules.md:15`: test fails, but functional behavior on a compliant device is identical.

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
