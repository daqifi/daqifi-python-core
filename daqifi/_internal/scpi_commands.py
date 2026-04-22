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

def reboot_device() -> ScpiMessage:
    """Reboot the device. Maps ``ScpiMessageProducer.RebootDevice``."""
    return ScpiMessage("SYSTem:REboot")


def get_device_info() -> ScpiMessage:
    """Request system information in protobuf format."""
    return ScpiMessage("SYSTem:SYSInfoPB?")


def force_bootloader() -> ScpiMessage:
    """Force the device into bootloader mode."""
    return ScpiMessage("SYSTem:FORceBoot")


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


def get_sd_logging_state() -> ScpiMessage:
    return ScpiMessage("SYSTem:STORage:SD:LOGging?")


def get_sd_file_list() -> ScpiMessage:
    return ScpiMessage("SYSTem:STORage:SD:LIST?")


def get_sd_file(file_name: str) -> ScpiMessage:
    """Retrieve a specific file from the SD card."""
    return ScpiMessage(f'SYSTem:STORage:SD:GET "{file_name}"')


def set_sd_logging_file_name(file_name: str) -> ScpiMessage:
    """Set the logging file name (creates or appends)."""
    return ScpiMessage(f'SYSTem:STORage:SD:LOGging "{file_name}"')


def delete_sd_file(file_name: str) -> ScpiMessage:
    """Delete a file from the SD card.

    Mirrors C# ``ArgumentException`` -> ``ValueError`` for empty/whitespace
    filenames (project convention for argument-validation exceptions).
    """
    if not file_name or not file_name.strip():
        raise ValueError("Filename cannot be null or empty.")
    return ScpiMessage(f'SYSTem:STORage:SD:DELete "{file_name}"')


def format_sd_card() -> ScpiMessage:
    """Destructively format the SD card."""
    return ScpiMessage("SYSTem:STORage:SD:FORmat")


def set_sd_max_file_size(num_bytes: int) -> ScpiMessage:
    """Set max file size before auto-split. ``num_bytes=0`` means default (3.9 GB).

    Mirrors C# ``ArgumentOutOfRangeException`` -> ``ValueError`` (project
    convention).
    """
    if num_bytes < 0:
        raise ValueError("Max file size cannot be negative.")
    return ScpiMessage(f"SYSTem:STORage:SD:MAXSize {num_bytes}")


def get_sd_max_file_size() -> ScpiMessage:
    return ScpiMessage("SYSTem:STORage:SD:MAXSize?")


def run_sd_benchmark(size: int) -> ScpiMessage:
    """Run an SD-card write-speed benchmark of ``size`` bytes."""
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


def start_streaming(frequency: int) -> ScpiMessage:
    """Start streaming at ``frequency`` Hz (1-1000)."""
    return ScpiMessage(f"SYSTem:StartStreamData {frequency}")


def stop_streaming() -> ScpiMessage:
    return ScpiMessage("SYSTem:StopStreamData")


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
    """Enable ADC channels via decimal bitmask string (e.g. ``"84"`` enables ch 2/4/6)."""
    return ScpiMessage(f"ENAble:VOLTage:DC {channel_set_string}")


def set_dio_port_direction(channel: int, direction: int) -> ScpiMessage:
    """Set channel direction (0 = input, 1 = output)."""
    return ScpiMessage(f"DIO:PORt:DIRection {channel},{direction}")


def set_dio_port_state(channel: int, value: float) -> ScpiMessage:
    """Set channel state (0 = low, 1 = high)."""
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
    return ScpiMessage(f'SYSTem:COMMunicate:LAN:SSID "{ssid}"')


def set_network_wifi_security_open() -> ScpiMessage:
    return ScpiMessage("SYSTem:COMMunicate:LAN:SECurity 0")


def set_network_wifi_security_wpa() -> ScpiMessage:
    return ScpiMessage("SYSTem:COMMunicate:LAN:SECurity 3")


def set_network_wifi_password(password: str) -> ScpiMessage:
    return ScpiMessage(f'SYSTem:COMMunicate:LAN:PASs "{password}"')


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
    """Set USB transparency mode (0 = disabled, 1 = enabled, bypassing SCPI console)."""
    return ScpiMessage(f"SYSTem:USB:SetTransparentMode {mode}")


def get_lan_chip_info() -> ScpiMessage:
    """Query LAN chip information (returns JSON ``{ChipId, FwVersion, BuildDate}``)."""
    return ScpiMessage("SYSTem:COMMunicate:LAN:GETChipInfo?")
