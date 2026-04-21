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

from daqifi.firmware import FirmwareVersion


pytestmark = pytest.mark.integration


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


# --- Divergence found 2026-04-21 ----------------------------------------------
# The Nyquist device under test returns its firmware version as `'01-02'`
# (a `<group>-<id>` shape, NOT semantic versioning). The C# `FirmwareVersion`
# in daqifi-core ASSUMES SemVer (`Major.Minor.Patch[suffix]`) and would
# return `false` from `TryParse` on this string. So the port matches the
# C# spec correctly — but the spec doesn't match the actual hardware.
#
# Decision needed (track in correspondence map):
#   (a) widen FirmwareVersion to accept hyphen-separated firmware IDs, OR
#   (b) introduce a separate `FirmwareRevision` type for the hyphenated shape
#       and only use FirmwareVersion for the SemVer path, OR
#   (c) update Nyquist firmware to report SemVer (out of scope for the port)
#
# Until then, the parsing tests xfail to keep the divergence visible.
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason=(
        "DIV: Nyquist device returns firmware as '01-02' (hyphen-separated), "
        "not SemVer. C# FirmwareVersion would also reject this — schema "
        "divergence between daqifi-core spec and Nyquist firmware response."
    ),
)
def test_firmware_version_parses_device_string(device):
    """End-to-end check: ported FirmwareVersion handles the actual device string.

    Currently xfail-strict because the Nyquist device's firmware string
    doesn't match the SemVer shape the C# port expects (see DIV note above).
    """
    fw_str = device.firmware_version
    parsed = FirmwareVersion.parse(fw_str)
    assert parsed is not None, (
        f"FirmwareVersion.parse() rejected device firmware string {fw_str!r}"
    )
    assert parsed.major >= 0
    assert parsed.minor >= 0
    assert parsed.patch >= 0


@pytest.mark.xfail(
    strict=True,
    reason="DIV: device firmware not in SemVer shape — same root as the parse test above.",
)
def test_device_firmware_version_is_release_or_meaningful_pre(device):
    """If the device firmware DID parse, check the pre-release label."""
    parsed = FirmwareVersion.parse(device.firmware_version)
    assert parsed is not None  # xfails here for the Nyquist hardware
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
