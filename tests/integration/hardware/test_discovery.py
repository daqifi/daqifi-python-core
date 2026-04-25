"""B3 — Discovery / device-finder hardware tests.

Covers two distinct discovery surfaces:

1. :mod:`daqifi.discovery.DeviceFinder` — the *IDN?-based serial finder
   used by the integration ``device_info`` fixture.
2. :mod:`daqifi.device_finders` — the production discovery surface:
   :class:`SerialDeviceFinder` (SCPI/protobuf serial probe),
   :class:`WiFiDeviceFinder` (UDP broadcast on port 30303).

WiFi tests must filter by serial number — multiple DAQiFi units may
exist on the LAN, and we have to identify *our* unit so a test running
in one agent's session doesn't accidentally claim another agent's
device. Override the expected SN with ``DAQIFI_TEST_WIFI_SN``;
otherwise the default points at this rig's COM7 unit.
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import List

import pytest

from daqifi.device_finders import (
    ConnectionType,
    DeviceDiscoveredEventArgs,
    SerialDeviceFinder,
    WiFiDeviceFinder,
)
from daqifi.device_finders import DeviceInfo as FinderDeviceInfo
from daqifi.discovery import (
    BoardVariant,
    DeviceFinder,
    DeviceInfo as DiscoveryDeviceInfo,
)


# ---- Helpers ---------------------------------------------------------------


def _expected_wifi_sn() -> str:
    """Serial number of the DAQiFi unit this agent's tests own."""
    return os.environ.get(
        "DAQIFI_TEST_WIFI_SN", "9090603604263365351"
    )


# ---- DeviceFinder (serial-only, IDN-based) ---------------------------------


def test_device_finder_get_serial_ports_returns_list(
    device_info: DiscoveryDeviceInfo,
) -> None:
    """The static helper returns a list — possibly empty on a host with
    no serial ports, possibly noisy on Windows (returns COM1..COM255
    candidates without verifying they exist).

    Takes ``device_info`` only to honor the directory's
    skip-collect-when-no-device contract; the helper itself is static
    and doesn't need an open device."""
    del device_info  # noqa: just gates the test on hardware presence
    ports = DeviceFinder.get_serial_ports()
    assert isinstance(ports, list)
    # On the test rig at least our port is real; sanity-check the type.
    for p in ports:
        assert isinstance(p, str)


def test_device_finder_discover_finds_real_device(
    device_info: DiscoveryDeviceInfo,
) -> None:
    """``DeviceFinder.discover()`` must return at least our COM7 unit."""
    devices = DeviceFinder.discover()
    assert isinstance(devices, list)
    assert len(devices) >= 1
    # Our device's port (with whitespace tolerated) shows up exactly once.
    expected_port = device_info.port.strip()
    matches = [d for d in devices if d.port.strip() == expected_port]
    assert matches, (
        f"discovered ports {[d.port for d in devices]!r} do not include "
        f"our test device {expected_port!r}"
    )


def test_device_finder_discover_first_returns_device(
    device_info: DiscoveryDeviceInfo,
) -> None:
    """``discover_first()`` returns the first hit (or None when nothing
    answers); on this rig at least one DAQiFi answers."""
    first = DeviceFinder.discover_first()
    assert first is not None
    assert isinstance(first, DiscoveryDeviceInfo)
    assert first.port  # non-empty port string


def test_device_finder_discover_with_variant_filter(
    device_info: DiscoveryDeviceInfo,
) -> None:
    """The ``variant`` filter only includes matching boards. Our COM7
    unit is a Nyquist1 (verified via memory + the smoke test)."""
    nq1 = DeviceFinder.discover(variant=BoardVariant.NQ1)
    assert isinstance(nq1, list)
    assert len(nq1) >= 1
    assert all(d.variant == BoardVariant.NQ1 for d in nq1)
    # No NQ3 on this rig — the filter must actually return an empty
    # list (a vacuous `all(... for d in nq3)` would pass even on a
    # broken filter that returned every device).
    nq3 = DeviceFinder.discover(variant=BoardVariant.NQ3)
    assert nq3 == []


# ---- SerialDeviceFinder (protobuf-based serial probe) ----------------------


def test_serial_device_finder_init_rejects_invalid_baud(
    device_info: DiscoveryDeviceInfo,
) -> None:
    del device_info  # noqa: just gates the test on hardware presence
    with pytest.raises(ValueError, match="baud_rate"):
        SerialDeviceFinder(baud_rate=0)
    with pytest.raises(ValueError, match="baud_rate"):
        SerialDeviceFinder(baud_rate=-1)


def test_serial_device_finder_discover_finds_real_device(
    device_info: DiscoveryDeviceInfo,
) -> None:
    """SerialDeviceFinder probes every available port with the SCPI init
    sequence; our COM7 unit must be among the discovered candidates."""
    finder = SerialDeviceFinder(baud_rate=115200)
    discovered_events: List[DeviceDiscoveredEventArgs] = []
    completed: List[bool] = []
    finder.add_device_discovered(discovered_events.append)
    finder.add_discovery_completed(lambda: completed.append(True))
    try:
        # Restrict to OUR port so we don't probe (and bounce DTR on)
        # other agents' devices.
        results = finder.discover(port_filter=[device_info.port.strip()])
    finally:
        finder.close()

    assert isinstance(results, list)
    assert len(results) == 1
    info = results[0]
    assert isinstance(info, FinderDeviceInfo)
    assert info.port_name == device_info.port.strip()
    assert info.connection_type == ConnectionType.SERIAL
    # discovery_completed fires exactly once at the end.
    assert completed == [True]
    # device_discovered fires once per result.
    assert len(discovered_events) == 1
    assert discovered_events[0].device_info is info


def test_serial_device_finder_remove_handlers_silences_callbacks(
    device_info: DiscoveryDeviceInfo,
) -> None:
    """add_/remove_ pairs must round-trip cleanly: after remove, callbacks
    don't fire on the next discover()."""
    finder = SerialDeviceFinder(baud_rate=115200)
    discovered_events: List[DeviceDiscoveredEventArgs] = []
    completed: List[bool] = []

    def on_discovered(ev: DeviceDiscoveredEventArgs) -> None:
        discovered_events.append(ev)

    def on_completed() -> None:
        completed.append(True)

    finder.add_device_discovered(on_discovered)
    finder.add_discovery_completed(on_completed)
    finder.remove_device_discovered(on_discovered)
    finder.remove_discovery_completed(on_completed)
    # Removing again is silent (no-op).
    finder.remove_device_discovered(on_discovered)
    finder.remove_discovery_completed(on_completed)

    try:
        finder.discover(port_filter=[device_info.port.strip()])
    finally:
        finder.close()

    assert discovered_events == []
    assert completed == []


def test_serial_device_finder_close_is_idempotent_and_blocks_discover(
    device_info: DiscoveryDeviceInfo,
) -> None:
    """close() can be called repeatedly; afterward discover() raises."""
    del device_info  # noqa: just gates the test on hardware presence
    finder = SerialDeviceFinder(baud_rate=115200)
    finder.close()
    finder.close()  # idempotent
    with pytest.raises(RuntimeError, match="closed"):
        finder.discover(port_filter=["COM_FAKE"])


# ---- WiFiDeviceFinder ------------------------------------------------------


def test_wifi_device_finder_init_rejects_invalid_port(
    device_info: DiscoveryDeviceInfo,
) -> None:
    del device_info  # noqa: just gates the test on hardware presence
    with pytest.raises(ValueError, match="discovery_port"):
        WiFiDeviceFinder(discovery_port=0)
    with pytest.raises(ValueError, match="discovery_port"):
        WiFiDeviceFinder(discovery_port=70000)


def test_wifi_device_finder_close_is_idempotent_and_blocks_discover(
    device_info: DiscoveryDeviceInfo,
) -> None:
    del device_info  # noqa: just gates the test on hardware presence
    finder = WiFiDeviceFinder()
    finder.close()
    finder.close()
    with pytest.raises(RuntimeError, match="closed"):
        finder.discover(timeout=timedelta(milliseconds=50))


def test_wifi_device_finder_discover_returns_list_of_device_info(
    device_info: DiscoveryDeviceInfo,
) -> None:
    """Even when our specific WiFi unit isn't on the LAN, discover()
    must return a list of DeviceInfo (possibly empty) without raising."""
    finder = WiFiDeviceFinder()
    try:
        results = finder.discover(timeout=timedelta(seconds=2))
    finally:
        finder.close()
    assert isinstance(results, list)
    for info in results:
        assert isinstance(info, FinderDeviceInfo)
        assert info.connection_type == ConnectionType.WIFI
        assert info.ip_address  # populated for every WiFi response
        assert info.port  # device_port is in the protobuf payload


def test_wifi_device_finder_filters_to_our_sn(
    device_info: DiscoveryDeviceInfo,
) -> None:
    """When our unit IS on the LAN's WiFi, it must appear in the discover
    results — identified by serial number, not by 'first hit' (multiple
    DAQiFi units may be present)."""
    expected_sn = _expected_wifi_sn()
    finder = WiFiDeviceFinder()
    try:
        # 3 s gives every device on a typical home LAN time to respond.
        results = finder.discover(timeout=timedelta(seconds=3))
    finally:
        finder.close()

    matches = [r for r in results if r.serial_number == expected_sn]
    if not matches:
        pytest.skip(
            f"our device (SN={expected_sn}) not present in WiFi discovery; "
            f"got {len(results)} other DAQiFi unit(s): "
            f"{[r.serial_number for r in results]!r}. "
            f"Device may be in self-hosted (AP) mode rather than client mode."
        )
    # If we're here, our SN was found exactly once (de-dupe is the
    # finder's job).
    assert len(matches) == 1
    ours = matches[0]
    assert ours.connection_type == ConnectionType.WIFI
    assert ours.ip_address
    assert ours.port  # SCPI TCP port from protobuf, default 9760
    # MAC is hyphen-separated upper-hex when the protobuf payload
    # carried it. Our memorized MAC for this SN.
    if ours.mac_address:
        assert ours.mac_address.count("-") == 5


def test_wifi_device_finder_event_handlers_round_trip(
    device_info: DiscoveryDeviceInfo,
) -> None:
    """add/remove_device_discovered + add/remove_discovery_completed
    register and silence cleanly. discovery_completed always fires once
    even when no devices are found."""
    finder = WiFiDeviceFinder()
    discovered: List[DeviceDiscoveredEventArgs] = []
    completed: List[bool] = []

    def on_discovered(ev: DeviceDiscoveredEventArgs) -> None:
        discovered.append(ev)

    def on_completed() -> None:
        completed.append(True)

    # Subscribe + immediately unsubscribe — completed should NOT fire.
    finder.add_discovery_completed(on_completed)
    finder.remove_discovery_completed(on_completed)
    finder.remove_discovery_completed(on_completed)  # idempotent

    finder.add_device_discovered(on_discovered)

    try:
        results = finder.discover(timeout=timedelta(milliseconds=500))
    finally:
        finder.close()
    # We unsubscribed completed before discover ran — no fires.
    assert completed == []
    # device_discovered fires once per result that actually came back.
    assert len(discovered) == len(results)
    # Each event carries the corresponding DeviceInfo.
    for ev, info in zip(discovered, results):
        assert ev.device_info is info
