"""SCPI command factories for DAQiFi devices.

Ports the C# ``ScpiMessageProducer`` static class
(``Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs`` @ e8d84af).
Per the project port convention, a static C# class of factory methods/properties
becomes a Python module of free functions. Each function returns a fresh
:class:`ScpiMessage` so callers get the same "always-new" semantics as
the upstream ``=> new ScpiMessage(...)`` properties.

Naming follows snake_case translation of the upstream PascalCase. Parameters
named ``bytes`` upstream become ``num_bytes`` to avoid shadowing the Python
builtin.
"""

from .comm import ScpiMessage
from .scpi import StreamInterface


# ---- System control ----

def get_idn() -> ScpiMessage:
    """Query the device identification string (``*IDN?``)."""
    return ScpiMessage("*IDN?")


def reset_device() -> ScpiMessage:
    """Reset the device to its default state (``*RST``)."""
    return ScpiMessage("*RST")


def get_last_error() -> ScpiMessage:
    """Pop one entry from the device's error queue (``SYSTem:ERRor?``)."""
    return ScpiMessage("SYSTem:ERRor?")


def get_system_info() -> ScpiMessage:
    """Request the human-readable system info text block (``SYSTem:INFO?``)."""
    return ScpiMessage("SYSTem:INFO?")


def reboot_device() -> ScpiMessage:
    """Reboot the device. Maps ``ScpiMessageProducer.RebootDevice``."""
    return ScpiMessage("SYSTem:REboot")


def get_device_info() -> ScpiMessage:
    """Request system information in protobuf format."""
    return ScpiMessage("SYSTem:SYSInfoPB?")


def force_bootloader() -> ScpiMessage:
    """Force the device into bootloader mode."""
    return ScpiMessage("SYSTem:FORceBoot")


def get_power_state() -> ScpiMessage:
    """Query the current power state (``SYSTem:POWer:STATe?``)."""
    return ScpiMessage("SYSTem:POWer:STATe?")


def set_power_state(state: int) -> ScpiMessage:
    """Set the device power state to ``state`` (0=Standby, 1=PoweredUp, ...)."""
    if isinstance(state, bool) or not isinstance(state, int):
        raise TypeError("state must be an int.")
    if state < 0:
        raise ValueError("state must be non-negative.")
    return ScpiMessage(f"SYSTem:POWer:STATe {state}")


def turn_device_on() -> ScpiMessage:
    """Turn the device on."""
    return ScpiMessage("SYSTem:POWer:STATe 1")


def disable_device_echo() -> ScpiMessage:
    """Disable command echo."""
    return ScpiMessage("SYSTem:ECHO -1")


def enable_device_echo() -> ScpiMessage:
    """Enable command echo."""
    return ScpiMessage("SYSTem:ECHO 1")


# ---- SD card storage ----

def enable_storage_sd() -> ScpiMessage:
    """Enable SD card logging (LAN must be disabled first)."""
    return ScpiMessage("SYSTem:STORage:SD:ENAble 1")


def disable_storage_sd() -> ScpiMessage:
    return ScpiMessage("SYSTem:STORage:SD:ENAble 0")


def get_sd_file_list() -> ScpiMessage:
    return ScpiMessage("SYSTem:STORage:SD:LIST?")


def get_sd_file_list_for_directory(directory: str) -> ScpiMessage:
    """List files under ``directory`` on the SD card. Empty/whitespace rejected."""
    if not directory or not directory.strip():
        raise ValueError("Directory cannot be null or empty.")
    return ScpiMessage(f"SYSTem:STORage:SD:LIST? {_scpi_quote(directory)}")


def _scpi_quote(value: str) -> str:
    """Quote ``value`` as a SCPI string literal.

    SCPI strings are double-quote delimited; embedded double-quotes
    are escaped by doubling them (``"`` → ``""``), per IEEE 488.2 /
    SCPI-99 §7.7. Reject CR/LF since those would terminate the SCPI
    command and allow injecting a second command line (Qodo PR 17
    pass 6 #7+#8 + pass 12 #1: was previously using backslash escape
    which is non-standard and parsers reject).
    """
    if value is None:
        raise TypeError("SCPI string value must not be None.")
    if "\r" in value or "\n" in value:
        raise ValueError("SCPI string must not contain CR/LF characters.")
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def get_sd_file(file_name: str) -> ScpiMessage:
    """Retrieve a specific file from the SD card."""
    if not file_name or not file_name.strip():
        raise ValueError("Filename cannot be null or empty.")
    return ScpiMessage(f"SYSTem:STORage:SD:GET {_scpi_quote(file_name)}")


def set_sd_logging_file_name(file_name: str) -> ScpiMessage:
    """Set the logging file name (creates or appends)."""
    if not file_name or not file_name.strip():
        raise ValueError("Filename cannot be null or empty.")
    # Firmware #311 rename: SCPI command was renamed from
    # ``SYSTem:STORage:SD:LOGging`` to ``SYSTem:STORage:SD:FILE``
    # (PR #5 on main). There was never a query form (#187).
    return ScpiMessage(f"SYSTem:STORage:SD:FILE {_scpi_quote(file_name)}")


def delete_sd_file(file_name: str) -> ScpiMessage:
    """Delete a file from the SD card.

    Mirrors C# ``ArgumentException`` -> ``ValueError`` for empty/whitespace
    filenames (project convention for argument-validation exceptions).
    """
    if not file_name or not file_name.strip():
        raise ValueError("Filename cannot be null or empty.")
    return ScpiMessage(f"SYSTem:STORage:SD:DELete {_scpi_quote(file_name)}")


def format_sd_card() -> ScpiMessage:
    """Destructively format the SD card."""
    return ScpiMessage("SYSTem:STORage:SD:FORmat")


def set_sd_max_file_size(num_bytes: int) -> ScpiMessage:
    """Set max file size before auto-split. ``num_bytes=0`` means default (3.9 GB).

    Mirrors C# ``ArgumentOutOfRangeException`` -> ``ValueError`` (project
    convention). Rejects bool inputs (which subclass int in Python) so
    ``True`` / ``False`` aren't formatted into the SCPI command as
    ``\"True\"`` / ``\"False\"`` (Qodo PR 17 pass 49 #2).
    """
    if isinstance(num_bytes, bool) or not isinstance(num_bytes, int):
        raise TypeError("num_bytes must be an int.")
    if num_bytes < 0:
        raise ValueError("Max file size cannot be negative.")
    return ScpiMessage(f"SYSTem:STORage:SD:MAXSize {num_bytes}")


def get_sd_max_file_size() -> ScpiMessage:
    return ScpiMessage("SYSTem:STORage:SD:MAXSize?")


def run_sd_benchmark(size: int) -> ScpiMessage:
    """Run an SD-card write-speed benchmark of ``size`` bytes."""
    if isinstance(size, bool) or not isinstance(size, int):
        raise TypeError("size must be an int.")
    if size <= 0:
        raise ValueError("Benchmark size must be positive.")
    return ScpiMessage(f"SYSTem:STORage:SD:BENCHmark {size}")


def get_sd_benchmark_results() -> ScpiMessage:
    return ScpiMessage("SYSTem:STORage:SD:BENCHmark?")


# ---- Streaming ----

def set_stream_interface(stream_interface: StreamInterface) -> ScpiMessage:
    """Set the streaming target interface.

    Mirrors C# ``ArgumentOutOfRangeException`` for unknown enum values.
    """
    try:
        validated = StreamInterface(stream_interface)
    except ValueError as exc:
        raise ValueError("Unknown stream interface.") from exc
    return ScpiMessage(f"SYSTem:STReam:INTerface {int(validated)}")


def get_stream_interface() -> ScpiMessage:
    return ScpiMessage("SYSTem:STReam:INTerface?")


def get_streaming_state() -> ScpiMessage:
    """``1`` while the device is streaming to any interface, else ``0``."""
    return ScpiMessage("SYSTem:STReam:DATA?")


def start_streaming(frequency: int) -> ScpiMessage:
    """Start streaming at ``frequency`` Hz (1-1000).

    Raises ``ValueError`` if ``frequency`` is outside the firmware's
    accepted range so the bug surfaces here rather than as silent
    no-streaming behaviour on the device (Qodo PR 17 pass 6 #5).
    """
    # Reject bool explicitly: bool is an int subclass in Python so
    # `True/False` would otherwise pass the range check as 1/0.
    if isinstance(frequency, bool) or not isinstance(frequency, int):
        raise TypeError("frequency must be an int.")
    if frequency < 1 or frequency > 1000:
        raise ValueError("frequency must be between 1 and 1000 Hz.")
    return ScpiMessage(f"SYSTem:STReam:START {frequency}")


def stop_streaming() -> ScpiMessage:
    return ScpiMessage("SYSTem:STReam:STOP")


def set_protobuf_stream_format() -> ScpiMessage:
    return ScpiMessage("SYSTem:STReam:FORmat 0")


def set_json_stream_format() -> ScpiMessage:
    return ScpiMessage("SYSTem:STReam:FORmat 1")


def set_csv_stream_format() -> ScpiMessage:
    return ScpiMessage("SYSTem:STReam:FORmat 2")


def get_stream_format() -> ScpiMessage:
    return ScpiMessage("SYSTem:STReam:FORmat?")


# ---- ADC / DIO ----

def enable_adc_channels(channel_set_string: str) -> ScpiMessage:
    """Enable ADC channels via decimal bitmask string (e.g. ``"84"`` enables ch 2/4/6).

    The bitmask is interpolated raw into the SCPI command, so reject
    any input that could break the command framing or inject a second
    SCPI line (Qodo PR 17 pass 7 #5 — SCPI command injection hardening).
    """
    if channel_set_string is None:
        raise TypeError("channel_set_string must not be None.")
    s = channel_set_string.strip()
    if not s:
        raise ValueError("channel_set_string must not be empty.")
    if "\r" in s or "\n" in s:
        raise ValueError("channel_set_string must not contain CR/LF.")
    # Accept hex digits since callers commonly pass "FF" / "FFFF".
    if not all(c in "0123456789abcdefABCDEF" for c in s):
        raise ValueError(
            "channel_set_string must be a decimal or hex bitmask string."
        )
    return ScpiMessage(f"ENAble:VOLTage:DC {s}")


def _validate_dio_channel(channel: int) -> None:
    """Type + range validation shared by the DIO setters (Qodo PR 17
    pass 14 #1+#2). Reject bool (int subclass), non-int, and negatives."""
    if isinstance(channel, bool) or not isinstance(channel, int):
        raise TypeError("channel must be an int.")
    if channel < 0:
        raise ValueError("channel must be non-negative.")


def set_dio_port_direction(channel: int, direction: int) -> ScpiMessage:
    """Set channel direction (0 = input, 1 = output)."""
    _validate_dio_channel(channel)
    if isinstance(direction, bool) or not isinstance(direction, int):
        raise TypeError("direction must be an int.")
    if direction not in (0, 1):
        raise ValueError("direction must be 0 or 1.")
    return ScpiMessage(f"DIO:PORt:DIRection {channel},{direction}")


def set_dio_port_state(channel: int, value) -> ScpiMessage:  # type: ignore[no-untyped-def]
    """Set channel state (0 = low, 1 = high).

    Accepts ``int`` or ``bool``; rejects floats and other types so
    e.g. ``1.5`` doesn't get sent to the firmware (Qodo PR 17 pass 7 #1).
    """
    _validate_dio_channel(channel)
    if isinstance(value, bool):
        value = 1 if value else 0
    if not isinstance(value, int) or value not in (0, 1):
        raise ValueError("value must be 0 or 1 (int or bool).")
    return ScpiMessage(f"DIO:PORt:STATe {channel},{value}")


def enable_dio_ports() -> ScpiMessage:
    return ScpiMessage("DIO:PORt:ENAble 1")


def disable_dio_ports() -> ScpiMessage:
    return ScpiMessage("DIO:PORt:ENAble 0")


# ---- Network / WiFi / LAN ----

def set_network_wifi_mode_self_hosted() -> ScpiMessage:
    """Self-hosted WiFi (device creates its own network)."""
    return ScpiMessage("SYSTem:COMMunicate:LAN:NETType 4")


def set_network_wifi_mode_existing() -> ScpiMessage:
    """Connect to an existing WiFi network."""
    return ScpiMessage("SYSTem:COMMunicate:LAN:NETType 1")


def set_network_wifi_ssid(ssid: str) -> ScpiMessage:
    return ScpiMessage(f"SYSTem:COMMunicate:LAN:SSID {_scpi_quote(ssid)}")


def set_network_wifi_security_open() -> ScpiMessage:
    return ScpiMessage("SYSTem:COMMunicate:LAN:SECurity 0")


def set_network_wifi_security_wpa() -> ScpiMessage:
    return ScpiMessage("SYSTem:COMMunicate:LAN:SECurity 3")


def set_network_wifi_password(password: str) -> ScpiMessage:
    return ScpiMessage(f"SYSTem:COMMunicate:LAN:PASs {_scpi_quote(password)}")


def disable_network_lan() -> ScpiMessage:
    return ScpiMessage("SYSTem:COMMunicate:LAN:ENAbled 0")


def enable_network_lan() -> ScpiMessage:
    return ScpiMessage("SYSTem:COMMunicate:LAN:ENAbled 1")


def apply_network_lan() -> ScpiMessage:
    return ScpiMessage("SYSTem:COMMunicate:LAN:APPLY")


def save_network_lan() -> ScpiMessage:
    return ScpiMessage("SYSTem:COMMunicate:LAN:SAVE")


def set_lan_firmware_update_mode() -> ScpiMessage:
    return ScpiMessage("SYSTem:COMMUnicate:LAN:FWUpdate")


# ---- Misc ----

def set_usb_transparency_mode(mode: int) -> ScpiMessage:
    """Set USB transparency mode (0 = disabled, 1 = enabled, bypassing SCPI console).

    Reject anything outside {0, 1} so the firmware never sees an
    undefined mode value (Qodo PR 17 pass 7 #3).
    """
    if isinstance(mode, bool) or not isinstance(mode, int):
        raise TypeError("mode must be an int.")
    if mode not in (0, 1):
        raise ValueError("mode must be 0 or 1.")
    return ScpiMessage(f"SYSTem:USB:SetTransparentMode {mode}")


def get_lan_chip_info() -> ScpiMessage:
    """Query LAN chip information (returns JSON ``{ChipId, FwVersion, BuildDate}``)."""
    return ScpiMessage("SYSTem:COMMunicate:LAN:GETChipInfo?")
