"""Firmware-related types: device firmware versions, update orchestration."""

from daqifi.firmware.bootloader import BootloaderProtocol, Pic32BootloaderProtocol
from daqifi.firmware.crc16 import Crc16
from daqifi.firmware.download_service import (
    FirmwareDownloadService,
    FirmwareUpdateCheckResult,
    ProgressReporter,
)
from daqifi.firmware.external_process import (
    ExternalProcessRequest,
    ExternalProcessResult,
    ExternalProcessRunner,
    SubprocessRunner,
)
from daqifi.firmware.intel_hex import HexFileRecord, IntelHexParser
from daqifi.firmware.lan_chip_info import LanChipInfo
from daqifi.firmware.release_info import FirmwareReleaseInfo
from daqifi.firmware.update_service import FirmwareUpdateService
from daqifi.firmware.version import (
    FirmwareRevision,
    FirmwareVersion,
    ParsedFirmware,
    parse_firmware_string,
)

__all__ = [
    "BootloaderProtocol",
    "Crc16",
    "ExternalProcessRequest",
    "ExternalProcessResult",
    "ExternalProcessRunner",
    "FirmwareDownloadService",
    "FirmwareReleaseInfo",
    "FirmwareRevision",
    "FirmwareUpdateCheckResult",
    "FirmwareUpdateService",
    "FirmwareVersion",
    "HexFileRecord",
    "IntelHexParser",
    "LanChipInfo",
    "ParsedFirmware",
    "Pic32BootloaderProtocol",
    "ProgressReporter",
    "SubprocessRunner",
    "parse_firmware_string",
]
