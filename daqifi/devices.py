"""DAQiFi device classes: DaqifiDevice, DaqifiStreamingDevice, DaqifiDeviceFactory.

Ports the public Device classes from ``Daqifi.Core/Device/`` (upstream
e8d84af). Distinct from :class:`daqifi.device.NyquistDevice`, which is
a higher-level user-facing API; this module provides the lower-level
transport+protocol-driven device abstraction the C# library is built
around.

Threading model: all I/O is synchronous (project convention). The
underlying :class:`StreamMessageConsumer` runs a background daemon
thread that delivers parsed protobuf messages via callback.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, List, Optional

from ._internal.comm import (
    ConnectionRetryOptions,
    DeviceMetadata,
    DeviceStatusEventArgs,
    GenericInboundMessage,
    HidDeviceInfo,
    MessageReceivedEventArgs,
    OutboundMessage,
    ScpiMessage,
    TransportStatusEventArgs,
)
from ._internal.scpi import ConnectionStatus, DeviceState, StreamInterface
from ._internal import scpi_commands
from .channels import ChannelsPopulatedEventArgs
from .communication import (
    LineBasedMessageParser,
    ProtobufMessageParser,
    StreamMessageConsumer,
)
from .protocol import ProtobufProtocolHandler
from .transport import (
    HidLibraryTransport,
    SerialStreamTransport,
    TcpStreamTransport,
    UdpTransport,
)


__all__ = [
    "AnalogChannel",
    "DigitalChannel",
    "DaqifiDevice",
    "DaqifiStreamingDevice",
    "DaqifiDeviceFactory",
]


# Lightweight data-only channel records populated from protobuf status.
# The `daqifi.channels` module's classes wrap a SerialInterface for
# user-facing read/write — different shape entirely. These ports of the
# upstream `AnalogChannel` / `DigitalChannel` exist to mirror the data
# the device exposes, not to drive I/O.

from dataclasses import dataclass


@dataclass
class AnalogChannel:
    """Port of upstream ``AnalogChannel`` — analog-input channel metadata."""
    index: int
    resolution: int = 65535
    name: str = ""
    direction: int = 0  # ChannelDirection.Input
    is_enabled: bool = False
    calibration_b: float = 0.0
    calibration_m: float = 1.0
    internal_scale_m: float = 1.0
    port_range: float = 1.0


@dataclass
class DigitalChannel:
    """Port of upstream ``DigitalChannel`` — digital I/O channel metadata."""
    index: int
    name: str = ""
    direction: int = 0  # ChannelDirection.Input
    is_enabled: bool = False


_DEFAULT_BAUD_RATE = 9600


def _default_message_factory(payload: bytes) -> Any:
    """Decode a wire-format `DaqifiOutMessage` payload.

    Lazily imports the generated protobuf module so consumers that
    don't need protobuf decoding (tests using fake messages, callers
    that inject their own factory) don't pay the import cost.

    Parses ``bytes`` into a real :class:`DaqifiOutMessage` instance —
    this is what populates :attr:`DaqifiDevice.metadata` after
    :meth:`DaqifiDevice.initialize`. Replaces the previous
    ``lambda b: b`` passthrough that left metadata empty on hardware.
    """
    from ._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
    return DaqifiOutMessage.FromString(payload)


# ---- DaqifiDevice ------------------------------------------------------------


class DaqifiDevice:
    """Base device implementation.

    Ports the C# ``DaqifiDevice`` (``Daqifi.Core/Device/DaqifiDevice.cs``).
    Owns a transport, message producer/consumer, and protocol handler.
    Satisfies the :class:`daqifi._internal.comm.Device` Protocol.

    Subclasses (e.g. :class:`DaqifiStreamingDevice`) layer
    domain-specific behaviour on top.
    """

    def __init__(
        self,
        name: str,
        transport: Optional[Any] = None,
        ip_address: Optional[str] = None,
        message_factory: Optional[Callable[[bytes], Any]] = None,
    ) -> None:
        self._name = name
        self._ip_address = ip_address
        self._transport = transport
        self._message_factory = message_factory or _default_message_factory
        self._status = ConnectionStatus.Disconnected
        self._state = DeviceState.Disconnected
        self._metadata = DeviceMetadata()
        self._channels: List[Any] = []
        self._timestamp_frequency: int = 0

        self._consumer: Optional[StreamMessageConsumer[Any]] = None
        self._protocol_handler: Optional[ProtobufProtocolHandler] = None
        self._is_initialized = False
        self._is_disconnecting = False
        self._disposed = False

        self._status_handlers: List[
            Callable[[DeviceStatusEventArgs], None]
        ] = []
        self._message_handlers: List[
            Callable[[MessageReceivedEventArgs[Any]], None]
        ] = []
        self._channels_populated_handlers: List[
            Callable[[ChannelsPopulatedEventArgs], None]
        ] = []

        if transport is not None:
            transport.add_status_changed(self._on_transport_status_changed)

    # ---- IDevice / Device Protocol surface --------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def ip_address(self) -> Optional[str]:
        return self._ip_address

    @property
    def is_connected(self) -> bool:
        return self._status == ConnectionStatus.Connected

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def state(self) -> DeviceState:
        return self._state

    @property
    def metadata(self) -> DeviceMetadata:
        return self._metadata

    @property
    def channels(self) -> List[Any]:
        # Read-only snapshot.
        return list(self._channels)

    @property
    def timestamp_frequency(self) -> int:
        return self._timestamp_frequency

    @property
    def transport(self) -> Optional[Any]:
        """The injected transport (read-only). ``None`` for stream-only init."""
        return self._transport

    # ---- event subscription -----------------------------------------------

    def add_status_changed(
        self, handler: Callable[[DeviceStatusEventArgs], None]
    ) -> None:
        self._status_handlers.append(handler)

    def remove_status_changed(
        self, handler: Callable[[DeviceStatusEventArgs], None]
    ) -> None:
        try:
            self._status_handlers.remove(handler)
        except ValueError:
            pass

    def add_message_received(
        self, handler: Callable[[MessageReceivedEventArgs[Any]], None]
    ) -> None:
        self._message_handlers.append(handler)

    def remove_message_received(
        self, handler: Callable[[MessageReceivedEventArgs[Any]], None]
    ) -> None:
        try:
            self._message_handlers.remove(handler)
        except ValueError:
            pass

    def add_channels_populated(
        self, handler: Callable[[ChannelsPopulatedEventArgs], None]
    ) -> None:
        self._channels_populated_handlers.append(handler)

    def remove_channels_populated(
        self, handler: Callable[[ChannelsPopulatedEventArgs], None]
    ) -> None:
        try:
            self._channels_populated_handlers.remove(handler)
        except ValueError:
            pass

    # ---- lifecycle --------------------------------------------------------

    def connect(self) -> None:
        self._raise_if_disposed()
        self._set_status(ConnectionStatus.Connecting)
        self._set_state(DeviceState.Connecting)

        try:
            if self._transport is not None:
                self._transport.connect()
                # Capture the stream once and validate it. A transport
                # that returns None from `.stream` after a successful
                # `.connect()` is broken — surface it now rather than
                # letting StreamMessageConsumer crash later (Qodo PR
                # 17 pass 4 #6).
                stream = self._transport.stream
                if stream is None:
                    raise RuntimeError(
                        "Transport reports connected but provided no "
                        "stream; cannot start message consumer."
                    )
                if self._consumer is None:
                    # Default factory decodes bytes into a real
                    # `DaqifiOutMessage` (see ``_default_message_factory``).
                    # Override per-device by passing ``message_factory``
                    # to the constructor — useful for tests that want
                    # raw-bytes passthrough or a custom proto class.
                    self._consumer = StreamMessageConsumer[Any](
                        stream,
                        ProtobufMessageParser(message_factory=self._message_factory),
                    )
                self._consumer.start()

            self._set_status(ConnectionStatus.Connected)
            self._set_state(DeviceState.Connected)
        except Exception:
            self._set_status(ConnectionStatus.Disconnected)
            self._set_state(DeviceState.Disconnected)
            raise

    def disconnect(self) -> None:
        self._is_disconnecting = True
        try:
            if self._consumer is not None:
                self._consumer.remove_message_received(self._on_inbound_message)
                self._consumer.stop_safely()
            if self._transport is not None:
                self._transport.disconnect()
        finally:
            self._set_status(ConnectionStatus.Disconnected)
            self._set_state(DeviceState.Disconnected)
            self._is_initialized = False
            self._is_disconnecting = False

    def send(self, message: Any) -> None:
        """Send an outbound message.

        Mirrors the C# ``Send<T>(IOutboundMessage<T>)`` -- only
        string-typed messages are dispatched today (the rest require a
        message-producer abstraction not yet ported); other types raise
        :class:`NotImplementedError` to match upstream behaviour.
        """
        if not self.is_connected:
            raise RuntimeError("Device is not connected.")
        if isinstance(message, ScpiMessage):
            data = message.get_bytes()
            stream = self._transport.stream if self._transport is not None else None
            if stream is None:
                raise NotImplementedError(
                    "Sending without a transport-backed stream is not supported."
                )
            stream.write(data)
            try:
                stream.flush()
            except AttributeError:
                pass
        else:
            raise NotImplementedError(
                "Direct message sending without a string-typed ScpiMessage is "
                "not yet implemented."
            )

    def initialize(self) -> None:
        """Run the standard SCPI init sequence.

        Ports the upstream ``InitializeAsync`` (sync here per project
        convention). Wires the protocol handler before sending the
        text-mode setup commands, then queries device info -- the
        protobuf consumer's status callback updates :attr:`metadata`.
        """
        if not self.is_connected:
            raise RuntimeError(
                "Device must be connected before initialization."
            )
        if self._is_initialized:
            return

        self._set_state(DeviceState.Initializing)
        try:
            self._protocol_handler = ProtobufProtocolHandler(
                status_message_handler=self._on_status_message_received,
                stream_message_handler=self._on_stream_message_received,
            )
            if self._consumer is not None:
                self._consumer.add_message_received(self._on_inbound_message)

            # Standard SCPI init sequence (mirrors upstream).
            self.send(scpi_commands.disable_device_echo())
            time.sleep(0.1)
            self.send(scpi_commands.stop_streaming())
            time.sleep(0.1)
            self.send(scpi_commands.turn_device_on())
            time.sleep(0.1)
            self.send(scpi_commands.set_protobuf_stream_format())
            time.sleep(0.1)
            # Pin the stream output to the same transport we connected
            # over. Without this, streaming bytes can be routed to the
            # last-configured interface (e.g. WiFi) and the consumer on
            # this transport sees nothing.
            interface = self._stream_interface_for_transport()
            if interface is not None:
                self.send(scpi_commands.set_stream_interface(interface))
                time.sleep(0.1)

            # Query device info -- the protobuf consumer dispatches the
            # response to OnStatusMessageReceived which fills metadata.
            # 1.0s wait: NQ1 over USB CDC needs ~500 ms for the
            # SYSInfoPB? push to land + parse + handler chain to fire;
            # 0.5s was right at the threshold and metadata sometimes
            # arrived just after initialize() returned (verified
            # 2026-04-23 hardware test). Doubling provides margin.
            self.send(scpi_commands.get_device_info())
            time.sleep(1.0)

            self._is_initialized = True
            self._set_state(DeviceState.Ready)
        except Exception:
            self._set_state(DeviceState.Error)
            raise

    def close(self) -> None:
        if self._disposed:
            return
        try:
            self.disconnect()
        except Exception:
            pass
        if self._consumer is not None:
            try:
                self._consumer.close()
            except Exception:
                pass
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        self._disposed = True

    # ---- channel population (called by status-message handler) ------------

    def populate_channels_from_status(self, message: Any) -> None:
        """Populate :attr:`channels` from a protobuf ``DaqifiOutMessage``.

        Mirrors upstream ``PopulateChannelsFromStatus``. The Python port
        treats ``message`` as duck-typed (the project's
        ``DaqifiOutMessage`` is :data:`typing.Any` by convention -- see
        :mod:`daqifi.protocol`). Any object with the standard protobuf
        field names works.
        """
        if message is None:
            raise TypeError("message must not be None.")

        timestamp_freq = getattr(message, "timestamp_freq", 0) \
            or getattr(message, "TimestampFreq", 0)
        if timestamp_freq:
            self._timestamp_frequency = int(timestamp_freq)

        self._channels.clear()

        analog_count = self._populate_analog_channels(message)
        digital_count = self._populate_digital_channels(message)

        # Snapshot before notifying so handlers can't mutate via _channels.
        snapshot = tuple(self._channels)
        args = ChannelsPopulatedEventArgs(
            channels=snapshot,
            analog_channel_count=analog_count,
            digital_channel_count=digital_count,
        )
        for h in list(self._channels_populated_handlers):
            try:
                h(args)
            except Exception:
                pass

    def _populate_analog_channels(self, message: Any) -> int:
        count = int(getattr(message, "analog_in_port_num", 0)
                    or getattr(message, "AnalogInPortNum", 0))
        if count == 0:
            return 0
        cal_b = self._field_list(message, "analog_in_cal_b", "AnalogInCalB")
        cal_m = self._field_list(message, "analog_in_cal_m", "AnalogInCalM")
        int_scale = self._field_list(
            message, "analog_in_int_scale_m", "AnalogInIntScaleM"
        )
        ranges = self._field_list(
            message, "analog_in_port_range", "AnalogInPortRange"
        )
        resolution = int(getattr(message, "analog_in_res", 0)
                         or getattr(message, "AnalogInRes", 0)) or 65535

        for i in range(count):
            channel = AnalogChannel(
                index=i,
                resolution=resolution,
                name=f"AI{i}",
                calibration_b=self._with_default(cal_b, i, 0.0),
                calibration_m=self._with_default(cal_m, i, 1.0),
                internal_scale_m=self._with_default(int_scale, i, 1.0),
                port_range=self._with_default(ranges, i, 1.0),
                is_enabled=False,
            )
            self._channels.append(channel)
        return count

    def _populate_digital_channels(self, message: Any) -> int:
        count = int(getattr(message, "digital_port_num", 0)
                    or getattr(message, "DigitalPortNum", 0))
        for i in range(count):
            self._channels.append(
                DigitalChannel(index=i, name=f"DIO{i}", is_enabled=False)
            )
        return count

    @staticmethod
    def _field_list(message: Any, snake: str, pascal: str) -> Optional[list]:
        return getattr(message, snake, None) or getattr(message, pascal, None)

    @staticmethod
    def _with_default(values, index: int, default):
        if values is None:
            return default
        try:
            return values[index]
        except (IndexError, TypeError, KeyError):
            return default

    # ---- internal callbacks -----------------------------------------------

    def _on_inbound_message(self, args: Any) -> None:
        # `args` shape mirrors MessageReceivedEventArgs; route through
        # the protocol handler if it accepts the message.
        message = getattr(args, "message", None)
        wrapped = GenericInboundMessage(data=message.data if message is not None else None)
        if self._protocol_handler is None or not self._protocol_handler.can_handle(wrapped):
            return
        # Hot path: streaming pumps thousands of these per second.
        # Prefer the synchronous `handle()` if the handler exposes it
        # (ProtobufProtocolHandler does — see Qodo PR 17 finding 4).
        # Fall back to the async path only for handlers that don't
        # expose a sync alternative.
        sync_handle = getattr(self._protocol_handler, "handle", None)
        if callable(sync_handle):
            sync_handle(wrapped)
            return
        # The streaming pipeline is wholly synchronous in this project.
        # If a custom protocol handler exposes only the async method,
        # fail loudly so the integrator either implements `handle()`
        # or runs their own async dispatcher — silently spinning up
        # a per-message event loop (the previous behavior) was a
        # latency cliff at high stream rates (Qodo PR 17 pass 2 #2).
        raise RuntimeError(
            "Protocol handler must expose a synchronous `handle()` "
            "method. Async-only handlers are not supported in the "
            "streaming pipeline because creating an asyncio event loop "
            "per inbound message destroys throughput at >100 Hz."
        )

    def _on_status_message_received(self, message: Any) -> None:
        # Fill metadata, populate channels, fan out to subscribers.
        try:
            self._metadata.update_from_protobuf(message)
        except Exception:
            pass
        try:
            self.populate_channels_from_status(message)
        except Exception:
            pass
        self._fire_message(message)

    def _on_stream_message_received(self, message: Any) -> None:
        self._fire_message(message)

    def _fire_message(self, payload: Any) -> None:
        from datetime import datetime, timezone
        args = MessageReceivedEventArgs(
            message=GenericInboundMessage(data=payload),
            raw_data=b"",
            timestamp=datetime.now(timezone.utc),
        )
        for h in list(self._message_handlers):
            try:
                h(args)
            except Exception:
                pass

    def _on_transport_status_changed(self, args: TransportStatusEventArgs) -> None:
        if args.is_connected:
            return
        if self._status == ConnectionStatus.Connected and not self._is_disconnecting:
            self._set_status(ConnectionStatus.Lost)

    # ---- helpers ----------------------------------------------------------

    def _set_status(self, value: ConnectionStatus) -> None:
        if self._status == value:
            return
        self._status = value
        evt = DeviceStatusEventArgs(status=value)
        for h in list(self._status_handlers):
            try:
                h(evt)
            except Exception:
                pass

    def _set_state(self, value: DeviceState) -> None:
        self._state = value

    def _stream_interface_for_transport(self) -> Optional[StreamInterface]:
        """Pick the SCPI stream-interface enum that matches the
        connected transport, so streaming data flows back over the
        same channel the caller connected on. Returns None for
        transports we don't recognise (caller can override with an
        explicit ``set_stream_interface`` send).
        """
        if self._transport is None:
            return None
        # Lazy-import the concrete transport classes here. Module-level
        # would create a true circular import via daqifi.transport →
        # daqifi.devices (DaqifiDeviceFactory.connect_serial constructs
        # transport instances) once we move the factory back into
        # transport.py — local import keeps the option open.
        from .transport import SerialStreamTransport, TcpStreamTransport
        if isinstance(self._transport, SerialStreamTransport):
            return StreamInterface.Usb
        if isinstance(self._transport, TcpStreamTransport):
            return StreamInterface.WiFi
        return None

    def _raise_if_disposed(self) -> None:
        if self._disposed:
            raise RuntimeError("DaqifiDevice has been closed.")


# ---- DaqifiStreamingDevice ---------------------------------------------------


class DaqifiStreamingDevice(DaqifiDevice):
    """Streaming-capable DAQiFi device.

    Ports the C# ``DaqifiStreamingDevice`` (``Daqifi.Core/Device/
    DaqifiStreamingDevice.cs``). Adds streaming-frequency control,
    start/stop streaming, and SCPI-driven SD-card / network /
    LAN-chip-info facade methods on top of :class:`DaqifiDevice`.

    SD-card list/download, network configuration apply, and similar
    multi-step operations are intentionally simpler than upstream --
    they send the corresponding SCPI command and rely on the consumer
    to surface the response. Hardware-coupled retry/timeout polish
    lives in :mod:`daqifi.device.NyquistDevice` for now.
    """

    _WIFI_MODULE_RESTART_DELAY_S = 2.0
    _SD_INTERFACE_SETTLE_DELAY_S = 0.1

    def __init__(
        self,
        name: str,
        transport: Optional[Any] = None,
        ip_address: Optional[str] = None,
        message_factory: Optional[Callable[[bytes], Any]] = None,
    ) -> None:
        super().__init__(
            name=name,
            transport=transport,
            ip_address=ip_address,
            message_factory=message_factory,
        )
        self._is_streaming = False
        self._is_logging_to_sd = False
        self._streaming_frequency = 100
        self._sd_card_files: List[str] = []

    # ---- streaming API (IStreamingDevice) ---------------------------------

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    @property
    def streaming_frequency(self) -> int:
        return self._streaming_frequency

    @streaming_frequency.setter
    def streaming_frequency(self, value: int) -> None:
        if value <= 0:
            raise ValueError("streaming_frequency must be positive.")
        self._streaming_frequency = int(value)

    def start_streaming(self) -> None:
        self.send(scpi_commands.start_streaming(self._streaming_frequency))
        self._is_streaming = True
        self._set_state(DeviceState.Streaming)

    def stop_streaming(self) -> None:
        self.send(scpi_commands.stop_streaming())
        self._is_streaming = False
        if self._state == DeviceState.Streaming:
            self._set_state(DeviceState.Ready)

    # ---- USB / transport-shape helpers -----------------------------------

    @property
    def is_usb_connection(self) -> bool:
        """``True`` when the underlying transport is a serial port (USB CDC)."""
        return isinstance(self._transport, SerialStreamTransport)

    @property
    def is_logging_to_sd_card(self) -> bool:
        return self._is_logging_to_sd

    @property
    def sd_card_files(self) -> List[str]:
        return list(self._sd_card_files)

    # ---- SD card facade (ISdCardOperations) -------------------------------

    def enable_sd_card_logging(self) -> None:
        self.send(scpi_commands.enable_storage_sd())
        self._is_logging_to_sd = True

    def disable_sd_card_logging(self) -> None:
        self.send(scpi_commands.disable_storage_sd())
        self._is_logging_to_sd = False

    def set_sd_logging_file_name(self, file_name: str) -> None:
        self.send(scpi_commands.set_sd_logging_file_name(file_name))

    def request_sd_card_file_list(self) -> None:
        """Send the LIST? command. Use a separate text-consumer flow
        (see :class:`daqifi.device.NyquistDevice`) to capture the
        response in detail; this method just dispatches the request."""
        self.send(scpi_commands.get_sd_file_list())
        time.sleep(self._SD_INTERFACE_SETTLE_DELAY_S)

    def delete_sd_file(self, file_name: str) -> None:
        self.send(scpi_commands.delete_sd_file(file_name))

    def format_sd_card(self) -> None:
        self.send(scpi_commands.format_sd_card())

    # ---- Network facade (INetworkConfigurable) ----------------------------

    def set_wifi_ssid(self, ssid: str) -> None:
        self.send(scpi_commands.set_network_wifi_ssid(ssid))

    def set_wifi_password(self, password: str) -> None:
        self.send(scpi_commands.set_network_wifi_password(password))

    def set_wifi_mode_self_hosted(self) -> None:
        self.send(scpi_commands.set_network_wifi_mode_self_hosted())

    def set_wifi_mode_existing(self) -> None:
        self.send(scpi_commands.set_network_wifi_mode_existing())

    def apply_network_configuration(self) -> None:
        """Apply pending LAN config; firmware then restarts the WiFi module."""
        self.send(scpi_commands.apply_network_lan())
        time.sleep(self._WIFI_MODULE_RESTART_DELAY_S)

    def save_network_configuration(self) -> None:
        self.send(scpi_commands.save_network_lan())

    # ---- LAN chip info (ILanChipInfoProvider) -----------------------------

    def request_lan_chip_info(self) -> None:
        self.send(scpi_commands.get_lan_chip_info())


# ---- DaqifiDeviceFactory -----------------------------------------------------


class DaqifiDeviceFactory:
    """Convenience factory for creating + initializing devices.

    Ports the C# ``DaqifiDeviceFactory`` static class. Async methods
    collapse to sync per project convention.

    Returns a :class:`DaqifiStreamingDevice` (matches upstream -- the
    factory never returns the bare base class).
    """

    @staticmethod
    def connect_tcp(
        host: str,
        port: int,
        device_name: str = "DaqifiDevice",
        retry_options: Optional[ConnectionRetryOptions] = None,
        initialize_device: bool = True,
        message_factory: Optional[Callable[[bytes], Any]] = None,
    ) -> DaqifiStreamingDevice:
        if not host or not host.strip():
            raise ValueError("Host cannot be null or empty.")
        DaqifiDeviceFactory._validate_port(port)
        transport = TcpStreamTransport(host, port)
        return DaqifiDeviceFactory._connect_with_transport(
            transport, device_name, retry_options, initialize_device, message_factory,
        )

    @staticmethod
    def connect_serial(
        port_name: str,
        baud_rate: int = _DEFAULT_BAUD_RATE,
        device_name: str = "DaqifiDevice",
        retry_options: Optional[ConnectionRetryOptions] = None,
        initialize_device: bool = True,
        message_factory: Optional[Callable[[bytes], Any]] = None,
    ) -> DaqifiStreamingDevice:
        if port_name is None:
            raise TypeError("port_name must not be None.")
        if not port_name.strip():
            raise ValueError("Port name cannot be empty or whitespace.")
        if baud_rate <= 0:
            raise ValueError("Baud rate must be greater than zero.")
        transport = SerialStreamTransport(port_name, baud_rate)
        return DaqifiDeviceFactory._connect_with_transport(
            transport, device_name, retry_options, initialize_device, message_factory,
        )

    @staticmethod
    def _validate_port(port: int) -> None:
        if port < 1 or port > 65535:
            raise ValueError("Port must be between 1 and 65535.")

    @staticmethod
    def _connect_with_transport(
        transport: Any,
        device_name: str,
        retry_options: Optional[ConnectionRetryOptions],
        initialize_device: bool,
        message_factory: Optional[Callable[[bytes], Any]] = None,
    ) -> DaqifiStreamingDevice:
        device: Optional[DaqifiStreamingDevice] = None
        try:
            transport.connect(retry_options=retry_options)
            device = DaqifiStreamingDevice(
                device_name, transport=transport, message_factory=message_factory,
            )
            device.connect()
            if initialize_device:
                device.initialize()
            return device
        except Exception:
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
            else:
                try:
                    transport.close()
                except Exception:
                    pass
            raise
