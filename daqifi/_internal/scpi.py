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


class ConnectionStatus(IntEnum):
    """Connection status of a device.

    Ports the C# ``ConnectionStatus`` enum from
    ``Daqifi.Core/Device/ConnectionStatus.cs``. Uses ``IntEnum`` to match
    the local convention established by :class:`ChannelType` and
    :class:`ChannelDirection` (the project standardizes on ``IntEnum`` --
    5 usages tree-wide -- over plain ``Enum``). Enum-member casing follows
    the upstream PascalCase, again mirroring the sibling enums above.
    """
    Disconnected = 0  # The device is disconnected
    Connecting = 1    # The device is in the process of connecting
    Connected = 2     # The device is connected
    Lost = 3          # The device connection has been lost
    Retrying = 4      # The device is retrying connection after a failure
    Failed = 5        # The device connection failed after all retry attempts


class DeviceState(IntEnum):
    """Operational state of a DAQiFi device.

    Ports the C# ``DeviceState`` enum from
    ``Daqifi.Core/Device/DeviceState.cs``. Distinct from
    :class:`ConnectionStatus` -- ``State`` tracks the high-level
    lifecycle (Initializing, Ready, Streaming, Error) while ``Status``
    tracks the transport-level connection (Disconnected, Connecting,
    Lost, ...).
    """
    Disconnected = 0  # not connected or has been disconnected
    Connecting = 1    # in the process of connecting
    Connected = 2     # connected but not yet initialized
    Initializing = 3  # running initialization sequence
    Ready = 4         # fully initialized and ready
    Streaming = 5     # actively streaming data
    Error = 6         # encountered an error


class StreamInterface(IntEnum):
    """Target interface for data streaming.

    Values correspond to firmware SCPI ``SYSTem:STReam:INTerface`` parameter.
    """
    Usb = 0      # Stream data over USB
    WiFi = 1     # Stream data over WiFi
    SdCard = 2   # Stream data to the SD card
    All = 3      # Stream data to all interfaces simultaneously


# NOTE: SCPI command vocabulary lives in ``scpi_commands`` — call the
# factory functions there (e.g. ``scpi_commands.get_idn()``) rather
# than constructing strings here. Closes #12. The previous
# ``System`` class (constants + static helpers, both duplicated by
# ``scpi_commands``) was deleted to remove the drift risk.


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
    """DAC SCPI commands (NQ3 only).

    Current firmware registers only ``SOURce:VOLTage:LEVel`` and its query
    for the DAC. ``ENAble:SOURce:DC`` and ``CONFigure:DAC:RANGe`` were removed
    (firmware 0af9efa86), and ``CONFigure:DAC:UPDATE`` is not registered.
    The setter takes ``<ch>,<volts>``: a space between them is answered with
    -101 Invalid character (#189).
    """
    VOLTAGE_QUERY = "SOUR:VOLT:LEV?"
    VOLTAGE_SET = "SOUR:VOLT:LEV"

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

    # One argument is a 16-bit mask for all channels; a channel and a
    # value address one channel (``DIO:PORt:STATe <ch>,<0|1>``, the same
    # for DIRection), and ``DIO:PORt:STATe? <ch>`` reads one.


# Streaming commands
class Streaming:
    """Data streaming SCPI commands"""
    START = "SYSTem:STReam:START"
    STOP = "SYSTem:STReam:STOP"
    STATUS_QUERY = "SYSTem:STReam:DATA?"
    FORMAT_SET = "SYSTem:STReam:FORmat"
    FORMAT_QUERY = "SYSTem:STReam:FORmat?"
    INTERFACE_SET = "SYSTem:STReam:INTerface"  # 0=USB, 1=WiFi, 2=SD, 3=All
    INTERFACE_QUERY = "SYSTem:STReam:INTerface?"
    STATS_QUERY = "SYSTem:STReam:STATS?"
    STATS_CLEAR = "SYSTem:STReam:STATS:CLEar"


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


def contains_scpi_error(lines) -> bool:
    """Return True if any line in ``lines`` looks like a SCPI error reply.

    Matches ``**ERROR`` (firmware extension prefix) and bare ``ERROR``
    (SCPI standard). Case-insensitive; tolerates leading whitespace so
    a line like ``"  **ERROR: -200, Execution error"`` still matches.

    Ports the C# ``ContainsScpiError`` from
    ``Daqifi.Core/Device/DaqifiStreamingDevice.cs``. Closes the part of
    daqifi/daqifi-python-core#22 that was missing from the Python port
    — without this helper, callers couldn't tell whether a captured
    text response indicated success or failure.
    """
    for line in lines:
        trimmed = line.lstrip()
        upper = trimmed.upper()
        if upper.startswith("**ERROR") or upper.startswith("ERROR"):
            return True
    return False


def find_system_error_reply(lines) -> Optional[str]:
    """The first ``SYST:ERR?`` reply in ``lines``, or ``None`` if there is none.

    A reply is ``<int>,<description>``, with the description quoted or not.
    A leading ``DAQIFI>`` prompt, in any case, is stripped first, whether it
    prefixes the reply or sits on its own line. ``0,No error`` is normalised
    to ``0,"No error"``. Shared by ``DaqifiDevice.drain_errors`` and the
    WiFi update's bridge handling, so both read replies the same way.
    """
    for raw in lines:
        candidate = raw.strip()
        if not candidate:
            continue
        # Case-insensitive prompt strip — firmware variants may emit
        # `Daqifi>` / `daqifi>` instead of the canonical `DAQIFI>`.
        if candidate.upper().startswith("DAQIFI>"):
            candidate = candidate[len("DAQIFI>"):].strip()
            if not candidate:
                continue
        code_str, sep, rest = candidate.partition(",")
        if not sep:
            continue
        try:
            code = int(code_str.strip())
        except ValueError:
            continue
        rest = rest.strip()
        # Non-empty content after the comma, quoted or not: firmware
        # variants omit the quotes (``-200,Execution error``).
        if not rest:
            continue
        if code == 0 and rest.replace('"', "").strip().lower() == "no error":
            return f'{code},"No error"'
        return candidate
    return None


def parse_system_error(response: str) -> tuple[int, str]:
    """Parse a ``SYST:ERR?`` response into (code, description).

    The SCPI standard format is ``<int code>,"<description>"`` — code 0
    with description ``"No error"`` means the device's error queue is
    empty (the previous operation succeeded). Non-zero codes describe
    actual errors.

    Returns ``(0, "No error")`` for malformed responses so callers
    treat them as the benign no-op state rather than spuriously
    raising. The most common malformed shape is whitespace / empty
    when the device echoed nothing back.
    """
    response = response.strip()
    if not response:
        return 0, "No error"
    code_str, _, desc = response.partition(",")
    try:
        code = int(code_str.strip())
    except ValueError:
        return 0, "No error"
    desc = desc.strip().strip('"')
    return code, desc


def check_setter_response(
    success: bool, error_response: str, op_description: str
) -> None:
    """Resolve a setter outcome from the error response captured by
    ``SerialInterface.send_set_with_error``.

    Some setters return ``success=False`` even on a successful write —
    typically because the firmware doesn't echo an explicit OK. The
    authoritative signal is the SYST:ERR? response captured DURING
    ``send_set_with_error`` — a second SYST:ERR? query would advance
    the device's error queue past the actual error and silently
    swallow it (Qodo PR #53 review #1).

    Three outcomes:
    * ``error_response`` parses as code 0 → success (closes #14).
    * Empty / whitespace-only → raise. A timeout / missing reply on
      the error query MUST NOT be treated as success (Qodo PR #53
      review #2 — would leave the device in an unknown state).
    * Any non-zero code → raise with the parsed (code, description).

    Lives in this module rather than in ``daqifi.channels`` so that
    higher-level callers (e.g. ``NyquistDevice.power_state``) can
    use it without importing across module-internal boundaries
    (Qodo PR #61 review #1).
    """
    # Imported here to avoid a hard dependency on the public
    # exceptions module from this internal SCPI helper file —
    # callers always go through here when they need this check.
    from daqifi.exceptions import CommandError

    if success:
        return
    if not error_response or not error_response.strip():
        raise CommandError(
            f"Failed to {op_description}: device did not respond to error query "
            "(possible timeout or transport disconnect)"
        )
    raw = error_response.strip()
    code_str, _, _ = raw.partition(",")
    no_error_literal = "no error" in raw.lower()
    try:
        int(code_str.strip())
        parsed_ok = True
    except ValueError:
        parsed_ok = False
    if not no_error_literal and not parsed_ok:
        raise CommandError(
            f"Failed to {op_description}: malformed error response "
            f"{error_response!r}"
        )
    code, desc = parse_system_error(error_response)
    if code == 0:
        # send_set returned False but the device explicitly reports no
        # error — the operation actually succeeded. Bug-fix path for #14.
        return
    raise CommandError(f"Failed to {op_description}: {code},{desc!r}")


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
