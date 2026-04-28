# portomatic apply prompt

**Plan:** `20260419T233425-port-sd-logging-name-v05`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free

## Step

- summary: Port ScpiMessageProducer.SetSdLoggingFileName (C# static method with string fileName arg -> Python parameterized SCPI command)
- classification: api-shape
- shard: Communication
- upstream_ref: (unset)
- downstream_target: daqifi/_internal/scpi.py

## Upstream file at this commit (full)

```csharp
using System;
using Daqifi.Core.Communication.Messages;

namespace Daqifi.Core.Communication.Producers;

/// <summary>
/// Produces SCPI (Standard Commands for Programmable Instruments) messages for DAQiFi devices.
/// </summary>
/// <remarks> 
/// Example usage:
/// <code>
/// // Send a command
/// messageProducer.Send(ScpiMessageProducer.Reboot);
/// 
/// // Send a query
/// messageProducer.Send(ScpiMessageProducer.SystemInfo);
/// </code>
/// </remarks>
public class ScpiMessageProducer
{
    /// <summary>
    /// Creates a command message to reboot the device.
    /// </summary>
    /// <remarks>
    /// This command will cause the device to perform a complete restart.
    /// Command: SYSTem:REboot
    /// Example: messageProducer.Send(ScpiMessageProducer.Reboot);
    /// </remarks>
    public static IOutboundMessage<string> RebootDevice => new ScpiMessage("SYSTem:REboot");

    /// <summary>
    /// Creates a query message to request system information in protocol buffer format.
    /// </summary>
    /// <remarks>
    /// Returns device information including firmware version, serial number, and capabilities.
    /// Command: SYSTem:SYSInfoPB?
    /// Example: messageProducer.Send(ScpiMessageProducer.SystemInfo);
    /// </remarks>
    public static IOutboundMessage<string> GetDeviceInfo => new ScpiMessage("SYSTem:SYSInfoPB?");

    /// <summary>
    /// Creates a command message to force the device into bootloader mode.
    /// </summary>
    /// <remarks>
    /// Command: SYSTem:FORceBoot
    /// Example: messageProducer.Send(ScpiMessageProducer.ForceBootloader);
    /// </remarks>
    public static IOutboundMessage<string> ForceBootloader => new ScpiMessage("SYSTem:FORceBoot");

    /// <summary>
    /// Creates a command message to turn the device on.
    /// </summary>
    /// <remarks>
    /// Command: SYSTem:POWer:STATe 1
    /// Example: messageProducer.Send(ScpiMessageProducer.DeviceOn);
    /// </remarks>
    public static IOutboundMessage<string> TurnDeviceOn => new ScpiMessage("SYSTem:POWer:STATe 1");

    /// <summary>
    /// Creates a command message to disable echo functionality.
    /// </summary>
    /// <remarks>
    /// When echo is disabled, the device will not echo back received commands.
    /// Command: SYSTem:ECHO -1
    /// Example: messageProducer.Send(ScpiMessageProducer.TurnOffEcho);
    /// </remarks>
    public static IOutboundMessage<string> DisableDeviceEcho => new ScpiMessage("SYSTem:ECHO -1");

    /// <summary>
    /// Creates a command message to enable echo functionality.
    /// </summary>
    /// <remarks>
    /// When echo is enabled, the device will echo back received commands.
    /// Command: SYSTem:ECHO 1
    /// Example: messageProducer.Send(ScpiMessageProducer.TurnOnEcho);
    /// </remarks>
    public static IOutboundMessage<string> EnableDeviceEcho => new ScpiMessage("SYSTem:ECHO 1");
    
    /// <summary>
    /// Creates a command message to enable SD card logging.
    /// </summary>
    /// <remarks>
    /// Note: LAN must be disabled first to enable SD card logging.
    /// Command: SYSTem:STORage:SD:ENAble 1
    /// Example: messageProducer.Send(ScpiMessageProducer.EnableSdCard);
    /// </remarks>
    public static IOutboundMessage<string> EnableStorageSd => new ScpiMessage("SYSTem:STORage:SD:ENAble 1");

    /// <summary>
    /// Creates a command message to disable SD card logging.
    /// </summary>
    /// <remarks>
    /// Command: SYSTem:STORage:SD:ENAble 0
    /// Example: messageProducer.Send(ScpiMessageProducer.DisableSdCard);
    /// </remarks>
    public static IOutboundMessage<string> DisableStorageSd => new ScpiMessage("SYSTem:STORage:SD:ENAble 0");

    /// <summary>
    /// Creates a query message to get the current SD card logging state.
    /// </summary>
    /// <remarks>
    /// Command: SYSTem:STORage:SD:LOGging?
    /// Example: messageProducer.Send(ScpiMessageProducer.GetSdLoggingState);
    /// </remarks>
    public static IOutboundMessage<string> GetSdLoggingState => new ScpiMessage("SYSTem:STORage:SD:LOGging?");

    /// <summary>
    /// Creates a query message to get the list of files on the SD card.
    /// </summary>
    /// <remarks>
    /// Command: SYSTem:STORage:SD:LIST?
    /// Example: messageProducer.Send(ScpiMessageProducer.GetSdFileList);
    /// </remarks>
    public static IOutboundMessage<string> GetSdFileList => new ScpiMessage("SYSTem:STORage:SD:LIST?");

    /// <summary>
    /// Creates a query message to retrieve a specific file from the SD card.
    /// </summary>
    /// <param name="fileName">The name of the file to retrieve. Must be enclosed in quotes.</param>
    /// <remarks>
    /// Command: SYSTem:STORage:SD:GET "filename.bin"
    /// Example: messageProducer.Send(ScpiMessageProducer.GetSdFile("data.bin"));
    /// </remarks>
    public static IOutboundMessage<string> GetSdFile(string fileName)
    {
        return new ScpiMessage($"SYSTem:STORage:SD:GET \"{fileName}\"");
    }

    /// <summary>
    /// Creates a command message to set the logging file name on the SD card.
    /// </summary>
    /// <param name="fileName">The name of the file to create or append to. Must be enclosed in quotes.</param>
    /// <remarks>
    /// The specified file will be created if it doesn't exist, or appended to if it already exists.
    /// Command: SYSTem:STORage:SD:LOGging "filename.bin"
    /// Example: messageProducer.Send(ScpiMessageProducer.SetSdLoggingFileName("data.bin"));
    /// </remarks>
    public static IOutboundMessage<string> SetSdLoggingFileName(string fileName)
    {
        return new ScpiMessage($"SYSTem:STORage:SD:LOGging \"{fileName}\"");
    }

    /// <summary>
    /// Creates a command message to delete a file from the SD card.
    /// </summary>
    /// <param name="fileName">The name of the file to delete.</param>
    /// <remarks>
    /// Command: SYSTem:STORage:SD:DELete "filename"
    /// Example: messageProducer.Send(ScpiMessageProducer.DeleteSdFile("data.bin"));
    /// </remarks>
    public static IOutboundMessage<string> DeleteSdFile(string fileName)
    {
        if (string.IsNullOrWhiteSpace(fileName))
        {
            throw new ArgumentException("Filename cannot be null or empty.", nameof(fileName));
        }

        return new ScpiMessage($"SYSTem:STORage:SD:DELete \"{fileName}\"");
    }

    /// <summary>
    /// Creates a command message to format the entire SD card.
    /// </summary>
    /// <remarks>
    /// Warning: This is a destructive operation that erases all data on the SD card.
    /// Command: SYSTem:STORage:SD:FORmat
    /// Example: messageProducer.Send(ScpiMessageProducer.FormatSdCard);
    /// </remarks>
    public static IOutboundMessage<string> FormatSdCard => new ScpiMessage("SYSTem:STORage:SD:FORmat");

    /// <summary>
    /// Creates a command message to set the maximum file size before auto-splitting on the SD card.
    /// </summary>
    /// <param name="bytes">The maximum file size in bytes. Use 0 for the default (3.9 GB).</param>
    /// <remarks>
    /// Command: SYSTem:STORage:SD:MAXSize bytes
    /// Example: messageProducer.Send(ScpiMessageProducer.SetSdMaxFileSize(1073741824)); // 1 GB
    /// </remarks>
    public static IOutboundMessage<string> SetSdMaxFileSize(long bytes)
    {
        if (bytes < 0)
        {
            throw new ArgumentOutOfRangeException(nameof(bytes), bytes, "Max file size cannot be negative.");
        }

        return new ScpiMessage($"SYSTem:STORage:SD:MAXSize {bytes}");
    }

    /// <summary>
    /// Creates a query message to get the current maximum file size setting for the SD card.
    /// </summary>
    /// <remarks>
    /// Command: SYSTem:STORage:SD:MAXSize?
    /// Example: messageProducer.Send(ScpiMessageProducer.GetSdMaxFileSize);
    /// </remarks>
    public static IOutboundMessage<string> GetSdMaxFileSize => new ScpiMessage("SYSTem:STORage:SD:MAXSize?");

    /// <summary>
    /// Creates a command message to run an SD card write speed benchmark.
    /// </summary>
    /// <param name="size">The size in bytes to benchmark.</param>
    /// <remarks>
    /// Command: SYSTem:STORage:SD:BENCHmark size
    /// Example: messageProducer.Send(ScpiMessageProducer.RunSdBenchmark(1048576)); // 1 MB
    /// </remarks>
    public static IOutboundMessage<string> RunSdBenchmark(long size)
    {
        if (size <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(size), size, "Benchmark size must be positive.");
        }

        return new ScpiMessage($"SYSTem:STORage:SD:BENCHmark {size}");
    }

    /// <summary>
    /// Creates a query message to get the SD card benchmark results.
    /// </summary>
    /// <remarks>
    /// Command: SYSTem:STORage:SD:BENCHmark?
    /// Example: messageProducer.Send(ScpiMessageProducer.GetSdBenchmarkResults);
    /// </remarks>
    public static IOutboundMessage<string> GetSdBenchmarkResults => new ScpiMessage("SYSTem:STORage:SD:BENCHmark?");

    /// <summary>
    /// Creates a command message to set the streaming target interface.
    /// </summary>
    /// <param name="streamInterface">The target interface for streaming data.</param>
    /// <remarks>
    /// Command: SYSTem:STReam:INTerface value
    /// Example: messageProducer.Send(ScpiMessageProducer.SetStreamInterface(StreamInterface.SdCard));
    /// </remarks>
    public static IOutboundMessage<string> SetStreamInterface(StreamInterface streamInterface)
    {
        if (!Enum.IsDefined(typeof(StreamInterface), streamInterface))
        {
            throw new ArgumentOutOfRangeException(nameof(streamInterface), streamInterface, "Unknown stream interface.");
        }

        return new ScpiMessage($"SYSTem:STReam:INTerface {(int)streamInterface}");
    }

    /// <summary>
    /// Creates a query message to get the current streaming target interface.
    /// </summary>
    /// <remarks>
    /// Command: SYSTem:STReam:INTerface?
    /// Example: messageProducer.Send(ScpiMessageProducer.GetStreamInterface);
    /// </remarks>
    public static IOutboundMessage<string> GetStreamInterface => new ScpiMessage("SYSTem:STReam:INTerface?");

    /// <summary>
    /// Creates a command message to start data streaming at the specified frequency.
    /// </summary>
    /// <param name="frequency">The streaming frequency in Hz (1-1000).</param>
    /// <remarks>
    /// Starts streaming data from enabled channels at the specified frequency.
    /// Command: SYSTem:StartStreamData frequency
    /// Example: messageProducer.Send(ScpiMessageProducer.StartStreaming(100)); // Stream at 100Hz
    /// </remarks>
    public static IOutboundMessage<string> StartStreaming(int frequency)
    {
        return new ScpiMessage($"SYSTem:StartStreamData {frequency}");
    }

    /// <summary>
    /// Creates a command message to stop data streaming.
    /// </summary>
    /// <remarks>
    /// Command: SYSTem:StopStreamData
    /// Example: messageProducer.Send(ScpiMessageProducer.StopStreaming);
    /// </remarks>
    public static IOutboundMessage<string> StopStreaming => new ScpiMessage("SYSTem:StopStreamData");

    /// <summary>
    /// Creates a command message to set the stream format to Protocol Buffer.
    /// </summary>
    /// <remarks>
    /// Sets the stream format to Protobuf (format = 0).
    /// Command: SYSTem:STReam:FORmat 0
    /// Example: messageProducer.Send(ScpiMessageProducer.SetProtobufStreamFormat);
    /// </remarks>
    public static IOutboundMessage<string> SetProtobufStreamFormat => new ScpiMessage("SYSTem:STReam:FORmat 0");

    /// <summary>
    /// Creates a command message to set the stream format to JSON.
    /// </summary>
    /// <remarks>
    /// Sets the stream format to JSON (format = 1).
    /// Command: SYSTem:STReam:FORmat 1
    /// Example: messageProducer.Send(ScpiMessageProducer.SetJsonStreamFormat);
    /// </remarks>
    public static IOutboundMessage<string> SetJsonStreamFormat => new ScpiMessage("SYSTem:STReam:FORmat 1");

 
```

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 10: `# System commands`
      `class System:`
      `IDN = "*IDN?"`
      `RST = "*RST"`
      `ERR = "SYST:ERR?"`
      `INFO = "SYST:INFO?"`
-   line 20: `# Power management`
        `POWER_STATE_QUERY = "SYST:POW:STAT?"`
        `POWER_STATE_SET = "SYST:POW:STAT"`
-   line 24: `# WiFi/LAN`
        `WIFI_SSID_QUERY = "SYST:COMM:LAN:SSID?"`
        `WIFI_SSID_SET = "SYST:COMM:LAN:SSID"`
        `WIFI_IP_QUERY = "SYST:COMM:LAN:ADDR?"`
        `WIFI_NETTYPE_QUERY = "SYST:COMM:LAN:NETTYPE?"  # Network type (1=STA client, 4=AP)`
        `WIFI_NETTYPE_SET = "SYST:COMM:LAN:NETTYPE"`
-   line 39: `# SD Card`
        `SD_STATUS_QUERY = "SYST:STOR:SD:STAT?"`
        `SD_ENABLE_SET = "SYST:STOR:SD:ENAble"`
        `SD_LOGGING_SET = "SYST:STOR:SD:LOGging"`
        `SD_LIST_QUERY = "SYST:STOR:SD:LISt?"`
        `SD_GET = "SYST:STOR:SD:GET"`
- line 51: `# Analog input (ADC) commands`
      `class AnalogInput:`
-   line 54: `# All channels enable/disable (bitmask)`
        `ENABLE_ALL_QUERY = "ENABle:VOLTage:DC?"`
        `ENABLE_ALL_SET = "ENABle:VOLTage:DC"`
-   line 58: `# Per-channel enable/disable`
        `ENABLE_CHANNEL_QUERY = "CONFigure:ADC:CHANnel?"`
        `ENABLE_CHANNEL_SET = "CONFigure:ADC:CHANnel"`
-   line 62: `# Channel range configuration`
        `RANGE_QUERY = "CONFigure:ADC:RANGe?"`
        `RANGE_SET = "CONFigure:ADC:RANGe"`
-   line 66: `# Sample rate`
        `SAMPLE_RATE_QUERY = "ACQuire:SAMPle:RATE?"`
        `SAMPLE_RATE_SET = "ACQuire:SAMPle:RATE"`
- line 71: `# Analog output (DAC) commands`
      `class AnalogOutput:`
-   line 74: `# Voltage control`
        `VOLTAGE_QUERY = "SOUR:VOLT:LEV?"`
        `VOLTAGE_SET = "SOUR:VOLT:LEV"`
-   line 78: `# Channel enable`
        `ENABLE_QUERY = "ENAB:SOUR:DC?"`
        `ENABLE_SET = "ENAB:SOUR:DC"`
-   line 82: `# Range configuration`
        `RANGE_QUERY = "CONF:DAC:RANG?"`
        `RANGE_SET = "CONF:DAC:RANG"`
-   line 86: `# Update outputs`
        `UPDATE = "CONF:DAC:UPDATE"`
-   line 89: `# Calibration`
        `CAL_SLOPE_SET = "CONF:DAC:chanCALM"`
        `CAL_OFFSET_SET = "CONF:DAC:chanCALB"`
        `CAL_SAVE = "CONF:DAC:SAVEcal"`
        `CAL_LOAD = "CONF:DAC:LOADcal"`
- line 96: `# Digital I/O commands`
      `class DigitalIO:`
-   line 99: `# Port-level commands (16-bit masks for all channels)`
        `PORT_DIRECTION_QUERY = "DIO:PORt:DIRection?"`
        `PORT_DIRECTION_SET = "DIO:PORt:DIRection"`
        `PORT_STATE_QUERY = "DIO:PORt:STATe?"`
        `PORT_STATE_SET = "DIO:PORt:STATe"`
        `PORT_ENABLE_QUERY = "DIO:PORt:ENAble?"`
- line 111: `# Streaming commands`
      `class Streaming:`
      `START = "SYSTem:StartStreamData"`
      `STOP = "SYSTem:StopStreamData"`
      `STATUS_QUERY = "SYSTem:StreamData?"`
      `FORMAT_SET = "SYSTem:STReam:FORmat"`
- line 125: `# Response parsing utilities`
      `def parse_bool(response: str) -> bool:`
      `response = response.strip()`
      `if response == "1":`
      `return True`
      `elif response == "0":`
-   line 156: `# Remove surrounding quotes if present`
        `if (response.startswith('"') and response.endswith('"')) or \`
        `(response.startswith("'") and response.endswith("'")):`
        `return response[1:-1]`
        `return response`
        `def parse_comma_separated(response: str) -> List[str]:`

```python
"""
SCPI command definitions and utilities for Nyquist devices.

Provides command constants and response parsing functions.
"""

from typing import Optional, List


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

## Map shard `Communication` — relevant entries

- file: `src/Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs` → `daqifi/_internal/scpi.py` (divergent)
  - notes: Both expose SCPI command strings, but architecturally different:
C# wraps each command in a ProducerMessage class instance with
.Data and .Format; Python exposes flat string constants on
grouped helper classes (System, AnalogInput, DigitalIO, etc.).
Per-command pairings tracked under `symbols`.


**Active divergences in this area:**
- `DIV-002` (api_shape): C# represents commands as Producer/Message objects (with Data + Format
fields). Python represents them as bare strings on grouped helper
classes. /portomatic-check should NOT propose adding ProducerMessage
to Python — this is intentional Pythonic flattening.

- `DIV-003` (behavioral): C# uses SCPI long form ("SYSTem:STORage:..."); Python uses short form
("SYST:STOR:..."). Both compliant; do not normalize either way.


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
