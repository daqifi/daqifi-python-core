"""Tests for :class:`FirmwareUpdateServiceOptions` (portomatic v0.15 round)."""

from __future__ import annotations

import pytest

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.update_options import FirmwareUpdateServiceOptions


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_match_upstream_constants():
    o = FirmwareUpdateServiceOptions()
    assert o.bootloader_vendor_id == 0x04D8
    assert o.bootloader_product_id == 0x003C
    assert o.poll_interval == pytest.approx(0.250)
    assert o.preparing_device_timeout == pytest.approx(20.0)
    assert o.waiting_for_bootloader_timeout == pytest.approx(45.0)
    assert o.connecting_timeout == pytest.approx(20.0)
    assert o.erasing_flash_timeout == pytest.approx(45.0)
    assert o.programming_timeout == pytest.approx(600.0)
    assert o.verifying_timeout == pytest.approx(45.0)
    assert o.jumping_to_application_timeout == pytest.approx(45.0)
    assert o.bootloader_response_timeout == pytest.approx(10.0)
    assert o.post_force_boot_delay == pytest.approx(1.5)
    assert o.post_lan_firmware_mode_delay == pytest.approx(2.0)
    assert o.post_wifi_reconnect_delay == pytest.approx(2.0)
    assert o.hid_connect_retry_count == 3
    assert o.hid_connect_retry_delay == pytest.approx(0.5)
    assert o.flash_write_retry_count == 3
    assert o.flash_write_retry_delay == pytest.approx(0.150)
    assert o.wifi_process_timeout == pytest.approx(480.0)
    assert o.wifi_flash_tool_file_name == "winc_flash_tool.cmd"
    assert (
        o.wifi_flash_tool_arguments_template
        == "/p {port} /d WINC1500 /k /e /i aio /w"
    )
    assert o.wifi_port_override is None


def test_defaults_validate_cleanly():
    # Out-of-the-box defaults must pass validation.
    FirmwareUpdateServiceOptions().validate()


# ---------------------------------------------------------------------------
# get_state_timeout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state, attr",
    [
        (FirmwareUpdateState.PREPARING_DEVICE, "preparing_device_timeout"),
        (FirmwareUpdateState.WAITING_FOR_BOOTLOADER, "waiting_for_bootloader_timeout"),
        (FirmwareUpdateState.CONNECTING, "connecting_timeout"),
        (FirmwareUpdateState.ERASING_FLASH, "erasing_flash_timeout"),
        (FirmwareUpdateState.PROGRAMMING, "programming_timeout"),
        (FirmwareUpdateState.VERIFYING, "verifying_timeout"),
        (FirmwareUpdateState.JUMPING_TO_APP, "jumping_to_application_timeout"),
    ],
)
def test_get_state_timeout_maps_each_timed_state(state, attr):
    o = FirmwareUpdateServiceOptions()
    assert o.get_state_timeout(state) == getattr(o, attr)


@pytest.mark.parametrize(
    "untimed_state",
    [
        FirmwareUpdateState.IDLE,
        FirmwareUpdateState.COMPLETE,
        FirmwareUpdateState.FAILED,
    ],
)
def test_get_state_timeout_falls_back_to_five_minutes(untimed_state):
    o = FirmwareUpdateServiceOptions()
    assert o.get_state_timeout(untimed_state) == pytest.approx(300.0)


def test_get_state_timeout_reflects_mutated_field():
    o = FirmwareUpdateServiceOptions()
    o.programming_timeout = 1234.5
    assert o.get_state_timeout(FirmwareUpdateState.PROGRAMMING) == pytest.approx(
        1234.5
    )


# ---------------------------------------------------------------------------
# Validation: positive timeouts/delays
# ---------------------------------------------------------------------------


POSITIVE_FIELDS = [
    "poll_interval",
    "preparing_device_timeout",
    "waiting_for_bootloader_timeout",
    "connecting_timeout",
    "erasing_flash_timeout",
    "programming_timeout",
    "verifying_timeout",
    "jumping_to_application_timeout",
    "bootloader_response_timeout",
    "post_force_boot_delay",
    "post_lan_firmware_mode_delay",
    "post_wifi_reconnect_delay",
    "hid_connect_retry_delay",
    "flash_write_retry_delay",
    "wifi_process_timeout",
]


@pytest.mark.parametrize("field_name", POSITIVE_FIELDS)
def test_zero_value_rejected_for_each_positive_field(field_name):
    o = FirmwareUpdateServiceOptions()
    setattr(o, field_name, 0.0)
    with pytest.raises(ValueError, match=field_name):
        o.validate()


@pytest.mark.parametrize("field_name", POSITIVE_FIELDS)
def test_negative_value_rejected_for_each_positive_field(field_name):
    o = FirmwareUpdateServiceOptions()
    setattr(o, field_name, -0.001)
    with pytest.raises(ValueError, match=field_name):
        o.validate()


# ---------------------------------------------------------------------------
# Validation: retry counts and USB ID ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_count", [0, -1, -100])
def test_hid_connect_retry_count_rejects_below_one(bad_count):
    o = FirmwareUpdateServiceOptions(hid_connect_retry_count=bad_count)
    with pytest.raises(ValueError, match="hid_connect_retry_count"):
        o.validate()


@pytest.mark.parametrize("bad_count", [0, -1])
def test_flash_write_retry_count_rejects_below_one(bad_count):
    o = FirmwareUpdateServiceOptions(flash_write_retry_count=bad_count)
    with pytest.raises(ValueError, match="flash_write_retry_count"):
        o.validate()


@pytest.mark.parametrize("bad_id", [-1, 0x10000, 0xFFFF + 1, 1_000_000])
def test_bootloader_vendor_id_range(bad_id):
    o = FirmwareUpdateServiceOptions(bootloader_vendor_id=bad_id)
    with pytest.raises(ValueError, match="bootloader_vendor_id"):
        o.validate()


@pytest.mark.parametrize("bad_id", [-1, 0x10000])
def test_bootloader_product_id_range(bad_id):
    o = FirmwareUpdateServiceOptions(bootloader_product_id=bad_id)
    with pytest.raises(ValueError, match="bootloader_product_id"):
        o.validate()


def test_usb_id_boundary_values_accepted():
    # 0 and 0xFFFF are the documented inclusive endpoints.
    FirmwareUpdateServiceOptions(
        bootloader_vendor_id=0, bootloader_product_id=0
    ).validate()
    FirmwareUpdateServiceOptions(
        bootloader_vendor_id=0xFFFF, bootloader_product_id=0xFFFF
    ).validate()


# ---------------------------------------------------------------------------
# Validation: WiFi flash tool file name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", ["", "   ", "\t", "\n"])
def test_wifi_flash_tool_file_name_rejects_blank(bad_name):
    o = FirmwareUpdateServiceOptions(wifi_flash_tool_file_name=bad_name)
    with pytest.raises(ValueError, match="wifi_flash_tool_file_name"):
        o.validate()


def test_wifi_port_override_can_be_set_and_cleared():
    o = FirmwareUpdateServiceOptions()
    assert o.wifi_port_override is None
    o.wifi_port_override = "COM7"
    assert o.wifi_port_override == "COM7"
    o.wifi_port_override = None
    assert o.wifi_port_override is None


# ---------------------------------------------------------------------------
# Mutability
# ---------------------------------------------------------------------------


def test_options_are_mutable_like_csharp_setters():
    # The C# class exposes ``{ get; set; }`` on every property — the Python
    # dataclass is intentionally mutable so callers can tweak knobs in place.
    o = FirmwareUpdateServiceOptions()
    o.programming_timeout = 999.0
    o.hid_connect_retry_count = 5
    o.wifi_flash_tool_file_name = "custom.cmd"
    assert o.programming_timeout == 999.0
    assert o.hid_connect_retry_count == 5
    assert o.wifi_flash_tool_file_name == "custom.cmd"
    o.validate()


def test_separate_instances_do_not_share_state():
    a = FirmwareUpdateServiceOptions()
    b = FirmwareUpdateServiceOptions()
    a.programming_timeout = 1.0
    assert b.programming_timeout == pytest.approx(600.0)


def test_constructor_accepts_keyword_overrides():
    o = FirmwareUpdateServiceOptions(
        bootloader_vendor_id=0x1234,
        bootloader_product_id=0x5678,
        programming_timeout=12.0,
        wifi_port_override="COM3",
    )
    assert o.bootloader_vendor_id == 0x1234
    assert o.bootloader_product_id == 0x5678
    assert o.programming_timeout == 12.0
    assert o.wifi_port_override == "COM3"
    o.validate()
