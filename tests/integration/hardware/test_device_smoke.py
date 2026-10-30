"""Live-device smoke test — proves the existing library + ported types work together.

This is the END-TO-END verification loop for portomatic ports:
- Connect to a real DAQiFi via the existing daqifi-python-core API
- Pull device metadata (manufacturer, model, serial, firmware version)
- Use PORTED types (e.g. `FirmwareVersion`) to parse / interpret that metadata
- Assert nothing throws and shapes match expectations

Auto-skips when no device is discoverable (see conftest). When
`DAQIFI_DEVICE_REQUIRED=1`, missing device fails the test instead.
"""
from __future__ import annotations

import pytest

from daqifi.firmware import (
    FirmwareRevision,
    FirmwareVersion,
    parse_firmware_string,
)


# Under tests/integration/hardware so the hardware-free run skips it: its
# device fixture probes every serial port (#205 item 4).
pytestmark = [pytest.mark.integration, pytest.mark.hardware]


def test_device_basic_introspection(device):
    """Smoke: connection alive + standard properties readable + non-empty."""
    assert device.is_connected is True
    assert device.manufacturer
    assert device.model
    assert device.serial_number
    assert device.firmware_version


def test_device_firmware_string_shape(device):
    """Capture the device's firmware string format so the port can be calibrated.

    Reports the actual string + repr so a parser failure in the next test
    has the data needed to widen the regex. Always passes — diagnostic only.
    """
    fw_str = device.firmware_version
    assert fw_str, "device returned empty firmware version string"
    # Print to capture in `pytest -s` runs and in the failure tail.
    print(f"\n[diagnostic] device firmware string: {fw_str!r} (len={len(fw_str)})")


def test_firmware_version_parses_device_string(device):
    """End-to-end: parse_firmware_string handles whatever shape the device returns.

    Nyquist NQ1/NQ2 reports a hyphenated revision (e.g. ``01-02``); other
    images may report SemVer (e.g. ``3.4.6b1``). Either is acceptable —
    parse_firmware_string picks the right type. Closes #9.
    """
    fw_str = device.firmware_version
    parsed = parse_firmware_string(fw_str)
    assert parsed is not None, (
        f"parse_firmware_string rejected device firmware string {fw_str!r}"
    )
    if isinstance(parsed, FirmwareRevision):
        assert parsed.group >= 0
        assert parsed.revision >= 0
    else:
        assert isinstance(parsed, FirmwareVersion)
        assert parsed.major >= 0
        assert parsed.minor >= 0
        assert parsed.patch >= 0


def test_device_firmware_version_is_release_or_meaningful_pre(device):
    """If the device returned a SemVer firmware version, check its pre-release label."""
    parsed = parse_firmware_string(device.firmware_version)
    assert parsed is not None
    if not isinstance(parsed, FirmwareVersion):
        pytest.skip(
            f"device firmware is not SemVer-shaped ({type(parsed).__name__}); "
            "pre-release label check does not apply"
        )
    if parsed.is_pre_release:
        known_labels = {
            "alpha", "a", "pre", "preview", "dev",
            "beta", "b",
            "rc", "releasecandidate",
        }
        label = (parsed.pre_label or "").lower()
        assert label in known_labels, (
            f"device reports unknown pre-release label {parsed.pre_label!r}"
        )
