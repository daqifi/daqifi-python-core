"""B2 — DaqifiDeviceFactory + DaqifiDevice / DaqifiStreamingDevice tests.

Drives the end-to-end factory path against the real serial port:
construct via :meth:`DaqifiDeviceFactory.connect_serial`, verify the
connected + initialized device exposes populated :attr:`metadata` and
:attr:`channels` (driven by the protobuf consumer's status-message
callback into :meth:`populate_channels_from_status`), and exercise the
event-subscription surface.

Lifecycle / argument-validation tests own their own device. Property
tests share a module-scoped device to keep the per-test ~2 s
initialize() cost from accumulating.
"""
from __future__ import annotations

from typing import Iterator, List

import pytest

from daqifi._internal.scpi import ConnectionStatus, DeviceState
from daqifi.channels import ChannelsPopulatedEventArgs
from daqifi._internal.comm import (
    DeviceStatusEventArgs,
    MessageReceivedEventArgs,
)
from daqifi.devices import (
    DaqifiDeviceFactory,
    DaqifiStreamingDevice,
)
from daqifi.discovery import DeviceInfo
from daqifi.transport import SerialStreamTransport


# ---- Argument validation (no hardware) --------------------------------------


def test_connect_serial_rejects_none_port_name() -> None:
    with pytest.raises(TypeError, match="port_name"):
        DaqifiDeviceFactory.connect_serial(port_name=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("port", ["", "   ", "\t\n"])
def test_connect_serial_rejects_blank_port_name(port: str) -> None:
    with pytest.raises(ValueError, match="empty or whitespace"):
        DaqifiDeviceFactory.connect_serial(port_name=port)


@pytest.mark.parametrize("baud", [0, -1, -115200])
def test_connect_serial_rejects_non_positive_baud(baud: int) -> None:
    with pytest.raises(ValueError, match="Baud rate"):
        DaqifiDeviceFactory.connect_serial(
            port_name="COM_FAKE", baud_rate=baud
        )


# ---- Lifecycle (each test owns its own device) -----------------------------


def _open_factory_device(port: str, **kwargs) -> DaqifiStreamingDevice:
    """Connect the factory against the resolved port (defaults match the
    project's USB CDC streaming convention).

    Discovery may return the port name with trailing whitespace
    (Windows registry quirk); normalize before opening.
    """
    return DaqifiDeviceFactory.connect_serial(
        port_name=port.strip(),
        baud_rate=115200,
        device_name="B2-test-device",
        **kwargs,
    )


def test_connect_serial_returns_streaming_device(
    device_info: DeviceInfo,
) -> None:
    """Happy-path factory call: returns a DaqifiStreamingDevice that's
    connected, initialized, and ready."""
    dev = _open_factory_device(device_info.port)
    try:
        assert isinstance(dev, DaqifiStreamingDevice)
        assert dev.is_connected is True
        assert dev.status == ConnectionStatus.Connected
        assert dev.state == DeviceState.Ready
        assert dev.name == "B2-test-device"
        # Serial transport: ip_address is left as the constructor default.
        assert dev.ip_address is None
    finally:
        dev.close()
    # After close: status must reflect disconnect; subsequent close is a no-op.
    assert dev.is_connected is False
    dev.close()


def test_connect_serial_without_initialize_stops_at_connected(
    device_info: DeviceInfo,
) -> None:
    """``initialize_device=False`` skips the SCPI init handshake — the
    device is wired and connected, but not Ready."""
    dev = _open_factory_device(device_info.port, initialize_device=False)
    try:
        assert dev.is_connected is True
        assert dev.status == ConnectionStatus.Connected
        # No initialize() means state stays at Connected (NOT Ready).
        assert dev.state == DeviceState.Connected
        # And no metadata / channels query was sent, so both stay empty.
        assert dev.metadata.firmware_version == ""
        assert dev.channels == []
    finally:
        dev.close()


def test_close_then_close_is_idempotent(device_info: DeviceInfo) -> None:
    dev = _open_factory_device(device_info.port)
    dev.close()
    dev.close()  # second close is a no-op
    assert dev.is_connected is False


# ---- Event subscription surface --------------------------------------------
#
# These tests open their own short-lived devices, so they MUST run before
# any test that activates the module-scoped ``factory_device`` fixture
# below — pytest holds COM7 open for the lifetime of that fixture, and
# a second open against the same port will raise PermissionError. Pytest
# runs tests in source order within a file, so keeping event tests above
# the module-fixture-using property tests is the entire ordering trick.


def test_channels_populated_event_fires_during_initialize(
    device_info: DeviceInfo,
) -> None:
    """add_channels_populated handlers must fire (at least) once
    during the initialize() handshake when populate_channels_from_status
    is invoked from the status-message handler."""
    events: List[ChannelsPopulatedEventArgs] = []

    def handler(ev: ChannelsPopulatedEventArgs) -> None:
        events.append(ev)

    # Connect WITHOUT initialize, register handler, then initialize so
    # we deterministically capture the populate event.
    dev = _open_factory_device(device_info.port, initialize_device=False)
    try:
        dev.add_channels_populated(handler)
        dev.initialize()
        assert events, "expected at least one channels-populated event"
        # Snapshot is a tuple; counts are non-negative.
        ev = events[-1]
        assert isinstance(ev.channels, tuple)
        assert ev.analog_channel_count >= 0
        assert ev.digital_channel_count >= 0
        # remove_channels_populated stops further notifications.
        dev.remove_channels_populated(handler)
        # Removing again is silent (no-op).
        dev.remove_channels_populated(handler)
    finally:
        dev.close()


def test_status_changed_handlers_observe_lifecycle(
    device_info: DeviceInfo,
) -> None:
    """add_status_changed handlers fire on Connection-status transitions
    (Connected -> Disconnected during disconnect()); remove silences them.

    NB: we subscribe AFTER the factory's connect (so we miss
    Connecting -> Connected) and drive the transition we observe via
    disconnect(). initialize() only mutates DeviceState, not status."""
    events: List[DeviceStatusEventArgs] = []

    def handler(ev: DeviceStatusEventArgs) -> None:
        events.append(ev)

    dev = _open_factory_device(device_info.port, initialize_device=False)
    try:
        dev.add_status_changed(handler)
        dev.disconnect()
        assert events, "expected at least one DeviceStatusEventArgs"
        assert all(isinstance(e, DeviceStatusEventArgs) for e in events)
        # The terminal status after disconnect must be Disconnected.
        assert events[-1].status == ConnectionStatus.Disconnected
        # Removing twice is silent.
        dev.remove_status_changed(handler)
        dev.remove_status_changed(handler)
    finally:
        dev.close()


def test_message_received_handlers_fire_during_initialize(
    device_info: DeviceInfo,
) -> None:
    """add_message_received handlers see at least one decoded message
    during initialize() (the SYS:INFO? response)."""
    events: List[MessageReceivedEventArgs] = []

    def handler(ev: MessageReceivedEventArgs) -> None:
        events.append(ev)

    dev = _open_factory_device(device_info.port, initialize_device=False)
    try:
        dev.add_message_received(handler)
        dev.initialize()
        assert events, "expected at least one MessageReceivedEventArgs"
        dev.remove_message_received(handler)
        dev.remove_message_received(handler)  # idempotent
    finally:
        dev.close()


# ---- Property tests (share a module-scoped device) -------------------------
#
# Defined LAST so the module-scoped fixture activates only after every
# function-scoped test above has already opened + closed COM7.


@pytest.fixture(scope="module")
def factory_device(device_info: DeviceInfo) -> Iterator[DaqifiStreamingDevice]:
    """One connect + initialize per test module — the property
    assertions all read from this single live device. Holds COM7 for
    the duration of the property tests."""
    dev = _open_factory_device(device_info.port)
    try:
        yield dev
    finally:
        dev.close()


def test_metadata_populated_after_initialize(
    factory_device: DaqifiStreamingDevice,
) -> None:
    """After initialize(), the SYS:INFO? response has been parsed and
    DeviceMetadata reflects the real firmware/hardware identity."""
    md = factory_device.metadata
    # Firmware version is the strongest signal that the protobuf
    # consumer round-trip worked end-to-end.
    assert md.firmware_version != "", (
        f"metadata.firmware_version is empty; full metadata={md!r}"
    )
    # Serial number arrives as an int from protobuf; the metadata path
    # stringifies it.
    assert md.serial_number != "", f"metadata.serial_number empty: {md!r}"
    # Part number drives device_type detection — populated when
    # device_pn is on the wire.
    assert md.part_number != "", f"metadata.part_number empty: {md!r}"


def test_channels_populated_after_initialize(
    factory_device: DaqifiStreamingDevice,
) -> None:
    """populate_channels_from_status, invoked from the status-message
    handler, should have produced a non-empty channel list."""
    channels = factory_device.channels
    assert len(channels) > 0, "no channels populated after initialize()"
    # Returns a snapshot list (not the internal one).
    other = factory_device.channels
    assert other == channels
    assert other is not channels


def test_timestamp_frequency_populated_after_initialize(
    factory_device: DaqifiStreamingDevice,
) -> None:
    """Firmware reports a non-zero timestamp tick frequency in the
    status protobuf; the device exposes it via .timestamp_frequency."""
    assert factory_device.timestamp_frequency > 0, (
        f"timestamp_frequency was {factory_device.timestamp_frequency!r}"
    )


def test_transport_is_serial_stream_transport(
    factory_device: DaqifiStreamingDevice,
) -> None:
    """The factory built and injected a SerialStreamTransport."""
    assert isinstance(factory_device.transport, SerialStreamTransport)
    assert factory_device.is_usb_connection is True
