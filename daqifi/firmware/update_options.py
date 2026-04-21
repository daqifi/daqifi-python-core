"""Configuration options for the firmware update service.

Ported from ``Daqifi.Core/Firmware/FirmwareUpdateServiceOptions.cs``.

Conventions applied:
- ``TimeSpan`` -> ``float`` seconds (Python idiom for short-duration knobs;
  callers can multiply by 1000 for milliseconds where needed).
- ``ArgumentOutOfRangeException`` / ``ArgumentException`` -> ``ValueError``.
- ``string.IsNullOrWhiteSpace`` -> ``not s or not s.strip()``.
- ``record``-style C# field defaults map to dataclass field defaults; the type
  is mutable in C# (``set;``) so the dataclass is left mutable too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from daqifi.enums import FirmwareUpdateState


# Default fall-through timeout used by ``get_state_timeout`` when the supplied
# state is not one of the timed lifecycle states (matches C# ``TimeSpan.FromMinutes(5)``).
_DEFAULT_STATE_TIMEOUT_SECONDS: float = 5 * 60.0


@dataclass
class FirmwareUpdateServiceOptions:
    """Configuration knobs for the firmware update service.

    All ``*_timeout`` and ``*_delay`` fields are durations expressed in seconds.
    """

    # USB identifiers ------------------------------------------------------
    bootloader_vendor_id: int = 0x04D8
    bootloader_product_id: int = 0x003C

    # Polling --------------------------------------------------------------
    poll_interval: float = 0.250  # 250 ms

    # Per-state timeouts ---------------------------------------------------
    preparing_device_timeout: float = 20.0
    waiting_for_bootloader_timeout: float = 45.0
    connecting_timeout: float = 20.0
    erasing_flash_timeout: float = 45.0
    programming_timeout: float = 10 * 60.0  # 10 minutes
    verifying_timeout: float = 45.0
    jumping_to_application_timeout: float = 45.0

    # Bootloader I/O -------------------------------------------------------
    bootloader_response_timeout: float = 10.0

    # Post-action settling delays -----------------------------------------
    post_force_boot_delay: float = 1.5  # 1500 ms
    post_lan_firmware_mode_delay: float = 2.0
    post_wifi_reconnect_delay: float = 2.0

    # HID retry policy -----------------------------------------------------
    hid_connect_retry_count: int = 3
    hid_connect_retry_delay: float = 0.5  # 500 ms

    # Flash retry policy ---------------------------------------------------
    flash_write_retry_count: int = 3
    flash_write_retry_delay: float = 0.150  # 150 ms

    # WiFi flashing --------------------------------------------------------
    wifi_process_timeout: float = 8 * 60.0  # 8 minutes
    wifi_flash_tool_file_name: str = "winc_flash_tool.cmd"
    wifi_flash_tool_arguments_template: str = (
        "/p {port} /d WINC1500 /k /e /i aio /w"
    )
    wifi_port_override: Optional[str] = None

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------
    def get_state_timeout(self, state: FirmwareUpdateState) -> float:
        """Return the configured timeout (seconds) for ``state``.

        Falls back to a 5-minute timeout when ``state`` is not one of the
        timed lifecycle states (matches the C# default-arm behaviour).
        """
        mapping = {
            FirmwareUpdateState.PREPARING_DEVICE: self.preparing_device_timeout,
            FirmwareUpdateState.WAITING_FOR_BOOTLOADER: self.waiting_for_bootloader_timeout,
            FirmwareUpdateState.CONNECTING: self.connecting_timeout,
            FirmwareUpdateState.ERASING_FLASH: self.erasing_flash_timeout,
            FirmwareUpdateState.PROGRAMMING: self.programming_timeout,
            FirmwareUpdateState.VERIFYING: self.verifying_timeout,
            FirmwareUpdateState.JUMPING_TO_APP: self.jumping_to_application_timeout,
        }
        return mapping.get(state, _DEFAULT_STATE_TIMEOUT_SECONDS)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Raise :class:`ValueError` when any option holds an invalid value."""
        positive_fields = (
            ("poll_interval", self.poll_interval),
            ("preparing_device_timeout", self.preparing_device_timeout),
            ("waiting_for_bootloader_timeout", self.waiting_for_bootloader_timeout),
            ("connecting_timeout", self.connecting_timeout),
            ("erasing_flash_timeout", self.erasing_flash_timeout),
            ("programming_timeout", self.programming_timeout),
            ("verifying_timeout", self.verifying_timeout),
            ("jumping_to_application_timeout", self.jumping_to_application_timeout),
            ("bootloader_response_timeout", self.bootloader_response_timeout),
            ("post_force_boot_delay", self.post_force_boot_delay),
            ("post_lan_firmware_mode_delay", self.post_lan_firmware_mode_delay),
            ("post_wifi_reconnect_delay", self.post_wifi_reconnect_delay),
            ("hid_connect_retry_delay", self.hid_connect_retry_delay),
            ("flash_write_retry_delay", self.flash_write_retry_delay),
            ("wifi_process_timeout", self.wifi_process_timeout),
        )
        for name, value in positive_fields:
            _validate_positive(value, name)

        if self.hid_connect_retry_count < 1:
            raise ValueError(
                f"hid_connect_retry_count must be at least 1 "
                f"(got {self.hid_connect_retry_count})."
            )

        if self.flash_write_retry_count < 1:
            raise ValueError(
                f"flash_write_retry_count must be at least 1 "
                f"(got {self.flash_write_retry_count})."
            )

        if not (0 <= self.bootloader_vendor_id <= 0xFFFF):
            raise ValueError(
                f"bootloader_vendor_id must be in range 0..65535 "
                f"(got {self.bootloader_vendor_id})."
            )

        if not (0 <= self.bootloader_product_id <= 0xFFFF):
            raise ValueError(
                f"bootloader_product_id must be in range 0..65535 "
                f"(got {self.bootloader_product_id})."
            )

        name = self.wifi_flash_tool_file_name
        if not name or not name.strip():
            raise ValueError("wifi_flash_tool_file_name cannot be empty.")


def _validate_positive(value: float, parameter_name: str) -> None:
    """Raise :class:`ValueError` when ``value`` is not strictly greater than zero."""
    if value <= 0:
        raise ValueError(
            f"{parameter_name} must be greater than zero (got {value})."
        )


__all__ = ["FirmwareUpdateServiceOptions"]
