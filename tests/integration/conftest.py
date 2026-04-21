"""Integration-test fixtures.

These tests connect to a real DAQiFi device. They auto-skip unless the
device is discoverable. Set `DAQIFI_DEVICE_PORT=/dev/ttyUSB0` (or
similar) to force a specific port; otherwise auto-discovery runs.

Set `DAQIFI_DEVICE_REQUIRED=1` to FAIL (rather than skip) when no
device is found — useful in CI runs that have a device attached.
"""
from __future__ import annotations

import os

import pytest

from daqifi.device import NyquistDevice
from daqifi.discovery import DeviceFinder, DeviceInfo


def _required() -> bool:
    return os.environ.get("DAQIFI_DEVICE_REQUIRED", "").lower() in ("1", "true", "yes")


def _explicit_port() -> str | None:
    return os.environ.get("DAQIFI_DEVICE_PORT") or None


@pytest.fixture(scope="session")
def device_info() -> DeviceInfo:
    """Discover a connected device, or skip if none present."""
    explicit = _explicit_port()
    if explicit:
        info = DeviceFinder._identify_device(explicit)  # noqa: SLF001 — internal but works
        if info is None:
            msg = f"DAQIFI_DEVICE_PORT={explicit!r} could not be identified as a DAQiFi device"
            if _required():
                pytest.fail(msg)
            pytest.skip(msg)
        return info

    info = DeviceFinder.discover_first()
    if info is None:
        msg = "no DAQiFi device discovered (set DAQIFI_DEVICE_PORT to force, or DAQIFI_DEVICE_REQUIRED=1 to fail)"
        if _required():
            pytest.fail(msg)
        pytest.skip(msg)
    return info


@pytest.fixture
def device(device_info: DeviceInfo):
    """Connect to the discovered device, yield it, then disconnect cleanly."""
    dev = NyquistDevice.connect(device_info)
    try:
        yield dev
    finally:
        try:
            dev.disconnect()
        except Exception:
            pass
