"""Tests for DaqifiDevice + DaqifiStreamingDevice + DaqifiDeviceFactory
ports (daqifi/devices.py @ upstream e8d84af). Hardware-coupled paths
(real connect, real init, real streaming) are exercised in
tests/integration/; this file covers the API contract: lifecycle state
machine, event subscription, channel population, SCPI dispatch,
factory validation."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from daqifi._internal.comm import (
    ConnectionRetryOptions,
    Device,
    DeviceStatusEventArgs,
    StreamingDevice,
)
from daqifi._internal.scpi import ConnectionStatus, DeviceState
from daqifi.devices import (
    AnalogChannel,
    DaqifiDevice,
    DaqifiDeviceFactory,
    DaqifiStreamingDevice,
    DigitalChannel,
)


pytestmark = pytest.mark.portomatic_generated


# ---- Fakes -------------------------------------------------------------------


class _FakeStream:
    def __init__(self):
        self.written: list = []

    def write(self, data: bytes) -> int:
        # Return the byte count so DaqifiDevice.send's write loop
        # can verify full transmission (Qodo PR 17 pass 11 #2 made
        # `None` from write() raise IOError).
        b = bytes(data)
        self.written.append(b)
        return len(b)

    def flush(self) -> None:
        pass

    def read(self, n: int) -> bytes:
        return b""

    @property
    def in_waiting(self) -> int:
        return 0


class _FakeTransport:
    def __init__(self):
        self.stream = _FakeStream()
        self._connected = False
        self.status_handlers: list = []
        self.connect_called = 0
        self.disconnect_called = 0
        self.close_called = 0

    def add_status_changed(self, h):
        self.status_handlers.append(h)

    def remove_status_changed(self, h):
        if h in self.status_handlers:
            self.status_handlers.remove(h)

    def connect(self, retry_options=None):
        self.connect_called += 1
        self._connected = True

    def disconnect(self):
        self.disconnect_called += 1
        self._connected = False

    def close(self):
        self.close_called += 1
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


# ---- DaqifiDevice ------------------------------------------------------------


class TestDaqifiDeviceLifecycle:
    def test_initial_state(self):
        d = DaqifiDevice(name="x", transport=None)
        assert d.name == "x"
        assert d.status is ConnectionStatus.Disconnected
        assert d.state is DeviceState.Disconnected
        assert not d.is_connected
        assert d.channels == []

    def test_satisfies_device_protocol(self):
        d = DaqifiDevice(name="x", transport=None)
        assert isinstance(d, Device)

    def test_connect_with_transport_advances_state(self):
        t = _FakeTransport()
        d = DaqifiDevice(name="x", transport=t)
        events: list = []
        d.add_status_changed(events.append)
        d.connect()
        try:
            assert t.connect_called == 1
            assert d.is_connected
            assert d.state is DeviceState.Connected
            statuses = [e.status for e in events]
            assert ConnectionStatus.Connecting in statuses
            assert ConnectionStatus.Connected in statuses
        finally:
            d.disconnect()

    def test_disconnect_resets_state(self):
        t = _FakeTransport()
        d = DaqifiDevice(name="x", transport=t)
        d.connect()
        d.disconnect()
        assert not d.is_connected
        assert d.state is DeviceState.Disconnected
        assert t.disconnect_called == 1

    def test_send_when_disconnected_raises(self):
        d = DaqifiDevice(name="x", transport=None)
        from daqifi._internal.scpi_commands import reboot_device
        with pytest.raises(RuntimeError, match="not connected"):
            d.send(reboot_device())

    def test_send_writes_scpi_to_stream(self):
        t = _FakeTransport()
        d = DaqifiDevice(name="x", transport=t)
        d.connect()
        try:
            from daqifi._internal.scpi_commands import reboot_device
            d.send(reboot_device())
            assert t.stream.written == [b"SYSTem:REboot\r\n"]
        finally:
            d.disconnect()

    def test_close_is_idempotent(self):
        t = _FakeTransport()
        d = DaqifiDevice(name="x", transport=t)
        d.close()
        d.close()
        with pytest.raises(RuntimeError, match="closed"):
            d.connect()


class TestDaqifiDeviceTransportStatus:
    def test_lost_status_on_unexpected_disconnect(self):
        t = _FakeTransport()
        d = DaqifiDevice(name="x", transport=t)
        d.connect()
        try:
            # Simulate transport dropping unexpectedly.
            from daqifi._internal.comm import TransportStatusEventArgs
            for h in t.status_handlers:
                h(TransportStatusEventArgs(
                    is_connected=False,
                    connection_info="lost",
                ))
            assert d.status is ConnectionStatus.Lost
        finally:
            try:
                d.disconnect()
            except Exception:
                pass

    def test_no_lost_status_during_intentional_disconnect(self):
        t = _FakeTransport()
        d = DaqifiDevice(name="x", transport=t)
        d.connect()
        d.disconnect()
        # After intentional disconnect, status must be Disconnected, not Lost.
        assert d.status is ConnectionStatus.Disconnected


class TestDaqifiDeviceChannelPopulation:
    def test_populates_analog_and_digital(self):
        d = DaqifiDevice(name="x", transport=None)
        msg = SimpleNamespace(
            timestamp_freq=1000,
            analog_in_port_num=2,
            analog_in_cal_b=[0.1, 0.2],
            analog_in_cal_m=[1.5, 2.5],
            analog_in_int_scale_m=[1.0, 1.0],
            analog_in_port_range=[5.0, 10.0],
            analog_in_res=65535,
            digital_port_num=3,
        )
        d.populate_channels_from_status(msg)
        assert d.timestamp_frequency == 1000
        assert sum(1 for c in d.channels if isinstance(c, AnalogChannel)) == 2
        assert sum(1 for c in d.channels if isinstance(c, DigitalChannel)) == 3
        assert d.channels[0].calibration_m == 1.5
        assert d.channels[2].name == "DIO0"

    def test_repopulation_clears_existing(self):
        d = DaqifiDevice(name="x", transport=None)
        msg1 = SimpleNamespace(
            timestamp_freq=0, analog_in_port_num=1, analog_in_cal_b=[0],
            analog_in_cal_m=[1], analog_in_int_scale_m=[1],
            analog_in_port_range=[1], analog_in_res=0, digital_port_num=0,
        )
        msg2 = SimpleNamespace(
            timestamp_freq=0, analog_in_port_num=0, analog_in_cal_b=[],
            analog_in_cal_m=[], analog_in_int_scale_m=[],
            analog_in_port_range=[], analog_in_res=0, digital_port_num=2,
        )
        d.populate_channels_from_status(msg1)
        assert len(d.channels) == 1
        d.populate_channels_from_status(msg2)
        assert len(d.channels) == 2
        assert all(isinstance(c, DigitalChannel) for c in d.channels)

    def test_event_fires_with_counts(self):
        d = DaqifiDevice(name="x", transport=None)
        events: list = []
        d.add_channels_populated(events.append)
        msg = SimpleNamespace(
            timestamp_freq=0, analog_in_port_num=2,
            analog_in_cal_b=[0, 0], analog_in_cal_m=[1, 1],
            analog_in_int_scale_m=[1, 1], analog_in_port_range=[1, 1],
            analog_in_res=0, digital_port_num=4,
        )
        d.populate_channels_from_status(msg)
        assert len(events) == 1
        assert events[0].analog_channel_count == 2
        assert events[0].digital_channel_count == 4

    def test_missing_calibration_uses_default(self):
        d = DaqifiDevice(name="x", transport=None)
        msg = SimpleNamespace(
            timestamp_freq=0, analog_in_port_num=3,
            # Only 1 calibration value supplied for 3 channels.
            analog_in_cal_b=[0.1], analog_in_cal_m=[1.5],
            analog_in_int_scale_m=[1], analog_in_port_range=[5.0],
            analog_in_res=0, digital_port_num=0,
        )
        d.populate_channels_from_status(msg)
        assert d.channels[0].calibration_m == 1.5
        # Channel 1 + 2 fall back to defaults.
        assert d.channels[1].calibration_m == 1.0
        assert d.channels[2].calibration_b == 0.0

    def test_pascal_case_field_names_also_work(self):
        d = DaqifiDevice(name="x", transport=None)
        msg = SimpleNamespace(
            TimestampFreq=500,
            AnalogInPortNum=1, AnalogInCalB=[0], AnalogInCalM=[2],
            AnalogInIntScaleM=[1], AnalogInPortRange=[1], AnalogInRes=4096,
            DigitalPortNum=0,
        )
        d.populate_channels_from_status(msg)
        assert d.timestamp_frequency == 500
        assert d.channels[0].calibration_m == 2
        assert d.channels[0].resolution == 4096


# ---- DaqifiStreamingDevice ---------------------------------------------------


class TestDaqifiStreamingDevice:
    def test_satisfies_streaming_device_protocol(self):
        sd = DaqifiStreamingDevice(name="s", transport=None)
        # StreamingDevice extends Device; both should be satisfied.
        assert isinstance(sd, Device)
        assert isinstance(sd, StreamingDevice)

    def test_initial_streaming_state(self):
        sd = DaqifiStreamingDevice(name="s", transport=None)
        assert sd.streaming_frequency == 100
        assert not sd.is_streaming
        assert not sd.is_logging_to_sd_card
        assert sd.sd_card_files == []
        assert not sd.is_usb_connection  # transport=None

    def test_streaming_frequency_setter_validates(self):
        sd = DaqifiStreamingDevice(name="s", transport=None)
        sd.streaming_frequency = 500
        assert sd.streaming_frequency == 500
        with pytest.raises(ValueError):
            sd.streaming_frequency = 0
        with pytest.raises(ValueError):
            sd.streaming_frequency = -10

    def test_start_streaming_sends_command_and_updates_state(self):
        t = _FakeTransport()
        sd = DaqifiStreamingDevice(name="s", transport=t)
        sd.connect()
        sd.streaming_frequency = 250
        sd.start_streaming()
        assert sd.is_streaming
        assert sd.state is DeviceState.Streaming
        assert any(b"StartStreamData 250" in w for w in t.stream.written)
        sd.stop_streaming()
        assert not sd.is_streaming
        assert sd.state is DeviceState.Ready
        sd.disconnect()

    def test_sd_logging_facade_dispatches_scpi(self):
        t = _FakeTransport()
        sd = DaqifiStreamingDevice(name="s", transport=t)
        sd.connect()
        try:
            sd.enable_sd_card_logging()
            assert sd.is_logging_to_sd_card
            assert any(b"SD:ENAble 1" in w for w in t.stream.written)

            sd.set_sd_logging_file_name("data.bin")
            # Firmware #311 rename: SD:LOGging → SD:FILE for the SET form.
            assert any(b'SD:FILE "data.bin"' in w for w in t.stream.written)

            sd.disable_sd_card_logging()
            assert not sd.is_logging_to_sd_card
        finally:
            sd.disconnect()

    def test_network_facade_dispatches_scpi(self):
        t = _FakeTransport()
        sd = DaqifiStreamingDevice(name="s", transport=t)
        sd.connect()
        try:
            sd.set_wifi_ssid("MyNet")
            sd.set_wifi_password("secret")
            sd.set_wifi_mode_existing()
            assert any(b'SSID "MyNet"' in w for w in t.stream.written)
            assert any(b'PASs "secret"' in w for w in t.stream.written)
            assert any(b"NETType 1" in w for w in t.stream.written)
        finally:
            sd.disconnect()

    def test_is_usb_connection_true_for_serial_transport(self):
        from daqifi.transport import SerialStreamTransport
        # Don't actually open the port; just check the type discriminator.
        sd = DaqifiStreamingDevice(
            name="s", transport=SerialStreamTransport("COM_X")
        )
        assert sd.is_usb_connection


# ---- DaqifiDeviceFactory -----------------------------------------------------


class TestDaqifiDeviceFactoryValidation:
    def test_connect_tcp_rejects_empty_host(self):
        with pytest.raises(ValueError, match="Host cannot be"):
            DaqifiDeviceFactory.connect_tcp("", 9760)
        with pytest.raises(ValueError, match="Host cannot be"):
            DaqifiDeviceFactory.connect_tcp("   ", 9760)

    def test_connect_tcp_rejects_invalid_port(self):
        with pytest.raises(ValueError, match="between 1 and 65535"):
            DaqifiDeviceFactory.connect_tcp("host", 0)
        with pytest.raises(ValueError, match="between 1 and 65535"):
            DaqifiDeviceFactory.connect_tcp("host", 70000)

    def test_connect_serial_rejects_empty_port_name(self):
        with pytest.raises(TypeError):
            DaqifiDeviceFactory.connect_serial(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="cannot be empty"):
            DaqifiDeviceFactory.connect_serial("")

    def test_connect_serial_rejects_non_positive_baud(self):
        with pytest.raises(ValueError, match="greater than zero"):
            DaqifiDeviceFactory.connect_serial("COM3", baud_rate=0)
        with pytest.raises(ValueError, match="greater than zero"):
            DaqifiDeviceFactory.connect_serial("COM3", baud_rate=-1)


# ---- Default protobuf message factory (#13) ----------------------------------


class TestDefaultMessageFactory:
    """The default factory decodes wire bytes into a real
    :class:`DaqifiOutMessage` so :attr:`DaqifiDevice.metadata`
    populates after :meth:`initialize`. Replaces the previous
    bytes-passthrough that left metadata empty on hardware (#13)."""

    def test_default_factory_imports_lazily(self):
        from daqifi.devices import _default_message_factory
        # Sanity: callable, takes bytes, returns an object.
        assert callable(_default_message_factory)

    def test_default_factory_decodes_real_payload(self):
        # Captured from a real NQ1 over UDP discovery (port 30303);
        # length prefix already stripped — this is the buffer
        # ``ProtobufMessageParser`` hands to the factory.
        from daqifi.devices import _default_message_factory
        payload = bytes.fromhex(
            "08e186ae900e400248015064880110900108ba01040000a040"
            "d8018020e0018020980210da0204c0a801a0e20204ffffff00"
            "ea0204c0a80101820306f8f005a495a7ba030b4441514946"
            "692d39354137c003a04c9204034e71319a0405322e302e30"
            "a20407332e342e366231a804a7d18390c69ea6947e"
        )
        # If this regression payload fails to decode, the schema in
        # ``_internal/proto/DaqifiOutMessage_pb2.py`` has diverged
        # from firmware — regenerate from the upstream ``.proto``.
        msg = _default_message_factory(payload)
        assert msg.device_pn == "Nq1"
        assert msg.device_fw_rev == "3.4.6b1"
        assert "DAQ" in msg.host_name.upper()

    def test_factory_override_replaces_default(self):
        # Tests that pass `message_factory=lambda b: b` should still be
        # able to opt out of protobuf decoding (e.g. integration tests
        # using a fake wire format).
        d = DaqifiStreamingDevice(name="dev", message_factory=lambda b: b)
        assert d._message_factory(b"raw") == b"raw"

    def test_factory_default_when_omitted(self):
        # No explicit factory → uses the protobuf decoder by default.
        from daqifi.devices import _default_message_factory
        d = DaqifiStreamingDevice(name="dev")
        assert d._message_factory is _default_message_factory


# ---- Stream interface auto-pin (#stream-route fix) ----------------------


def _make_fake_transport(real_transport_cls):
    """Build a subclass of ``real_transport_cls`` that bypasses the
    real init (which would touch pyserial / sockets) but still passes
    ``isinstance`` checks against the real class. Needed because the
    auto-pin logic in `_stream_interface_for_transport` uses
    ``isinstance`` to identify the transport kind."""
    class _Fake(real_transport_cls):
        def __init__(self):
            # Set the private backing attributes the real `stream` /
            # `is_connected` properties read — bypass the real
            # __init__ which would touch pyserial/sockets.
            self._stream = _FakeStream()
            self._connected = False
            self.status_handlers = []

        @property
        def stream(self): return self._stream
        @property
        def is_connected(self): return self._connected

        def add_status_changed(self, h): self.status_handlers.append(h)
        def remove_status_changed(self, h):
            if h in self.status_handlers: self.status_handlers.remove(h)
        def connect(self, retry_options=None): self._connected = True
        def disconnect(self): self._connected = False
        def close(self): self._connected = False

    return _Fake()


class TestStreamInterfaceAutoPin:
    """Regression for the 2026-04-23 hardware finding: streaming bytes
    were going to the wrong interface (last-configured one) because
    `initialize()` didn't pin the stream interface to the connected
    transport. Verifies the fix sends the right SCPI."""

    def _scpi_text_sent(self, dev) -> str:
        return b"".join(dev._transport.stream.written).decode("ascii", "replace")

    def test_serial_transport_pins_stream_interface_to_usb(self):
        from daqifi.transport import SerialStreamTransport
        t = _make_fake_transport(SerialStreamTransport)
        dev = DaqifiStreamingDevice(name="d", transport=t)
        dev.connect()
        dev.initialize()
        sent = self._scpi_text_sent(dev)
        assert "SYSTem:STReam:INTerface 0" in sent, (
            f"USB-transport device did not pin stream interface to 0; "
            f"sent: {sent!r}"
        )

    def test_tcp_transport_pins_stream_interface_to_wifi(self):
        from daqifi.transport import TcpStreamTransport
        t = _make_fake_transport(TcpStreamTransport)
        dev = DaqifiStreamingDevice(name="d", transport=t)
        dev.connect()
        dev.initialize()
        sent = self._scpi_text_sent(dev)
        assert "SYSTem:STReam:INTerface 1" in sent, (
            f"TCP-transport device did not pin stream interface to 1; "
            f"sent: {sent!r}"
        )

    def test_unknown_transport_omits_stream_interface(self):
        # Unknown transport class — leave stream interface alone.
        t = _FakeTransport()  # class name is "_FakeTransport"
        dev = DaqifiStreamingDevice(name="d", transport=t)
        dev.connect()
        dev.initialize()
        sent = self._scpi_text_sent(dev)
        assert "SYSTem:STReam:INTerface" not in sent, (
            f"unknown transport should not send STReam:INTerface; "
            f"sent: {sent!r}"
        )
