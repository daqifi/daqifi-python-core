"""Protocol handlers for DAQiFi device messages.

Ported from upstream ``Daqifi.Core.Device.Protocol.ProtobufProtocolHandler``
(``src/Daqifi.Core/Device/Protocol/ProtobufProtocolHandler.cs`` @ e8d84af).
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable

from daqifi.communication import InboundMessage
from daqifi.enums import ProtobufMessageType


@runtime_checkable
class IProtocolHandler(Protocol):
    """Structural contract for objects that can dispatch inbound messages."""

    def can_handle(self, message: InboundMessage[Any]) -> bool:
        """Return True if this handler can process ``message``."""
        ...

    async def handle_async(self, message: InboundMessage[Any]) -> None:
        """Process ``message`` asynchronously."""
        ...


# TODO(portomatic): shallow-stub for upstream
# src/Daqifi.Core/Communication/Messages/DaqifiOutMessage.cs — the real protobuf
# class is duck-typed here. Any object exposing the field names referenced
# below (``device_status``, ``digital_port_num``, ``analog_in_port_num``,
# ``analog_out_port_num``, ``msg_time_stamp``, ``analog_in_data``,
# ``analog_in_data_float``, ``digital_data``) is acceptable.
DaqifiOutMessage = Any


class ProtobufProtocolHandler:
    """Handles protobuf-based protocol messages from DAQiFi devices.

    Routes a ``DaqifiOutMessage`` payload to one of four optional callbacks
    based on a content-driven type detection (see :meth:`detect_message_type`).
    """

    def __init__(
        self,
        status_message_handler: Optional[Callable[[DaqifiOutMessage], None]] = None,
        stream_message_handler: Optional[Callable[[DaqifiOutMessage], None]] = None,
        sd_card_message_handler: Optional[Callable[[DaqifiOutMessage], None]] = None,
        error_message_handler: Optional[Callable[[DaqifiOutMessage], None]] = None,
    ) -> None:
        self._status_message_handler = status_message_handler
        self._stream_message_handler = stream_message_handler
        self._sd_card_message_handler = sd_card_message_handler
        self._error_message_handler = error_message_handler

    def can_handle(self, message: InboundMessage[Any]) -> bool:
        """Return True iff ``message.data`` looks like a DaqifiOutMessage.

        Duck-typed: presence of the ``device_status`` attribute is treated as
        the discriminator since the upstream type check (``is DaqifiOutMessage``)
        has no clean Python analog without coupling to a real protobuf class.
        """
        return _is_daqifi_out_message(message.data)

    async def handle_async(self, message: InboundMessage[Any]) -> None:
        """Dispatch ``message.data`` to the matching handler, if any."""
        pb_message = message.data
        if not _is_daqifi_out_message(pb_message):
            return

        message_type = self.detect_message_type(pb_message)

        if message_type is ProtobufMessageType.STATUS:
            if self._status_message_handler is not None:
                self._status_message_handler(pb_message)
        elif message_type is ProtobufMessageType.STREAM:
            if self._stream_message_handler is not None:
                self._stream_message_handler(pb_message)
        elif message_type is ProtobufMessageType.SD_CARD:
            if self._sd_card_message_handler is not None:
                self._sd_card_message_handler(pb_message)
        elif message_type is ProtobufMessageType.ERROR:
            if self._error_message_handler is not None:
                self._error_message_handler(pb_message)
        # ProtobufMessageType.UNKNOWN and any unmapped value: no-op
        # (could log here if needed)

    @staticmethod
    def detect_message_type(message: DaqifiOutMessage) -> ProtobufMessageType:
        """Detect the type of a protobuf message based on its contents."""
        # Status messages contain device configuration information
        if ProtobufProtocolHandler._is_status_message(message):
            return ProtobufMessageType.STATUS

        # Streaming messages contain analog/digital data with timestamps
        if ProtobufProtocolHandler._is_stream_message(message):
            return ProtobufMessageType.STREAM

        # SD card messages are typically text-based and handled separately
        # (placeholder - actual SD card messages come as text responses, not protobuf)

        # Error messages contain a non-zero device status
        if getattr(message, "device_status", 0) != 0:
            return ProtobufMessageType.ERROR

        return ProtobufMessageType.UNKNOWN

    @staticmethod
    def _is_status_message(message: DaqifiOutMessage) -> bool:
        """A message is a status message if it advertises any port count."""
        return (
            getattr(message, "digital_port_num", 0) != 0
            or getattr(message, "analog_in_port_num", 0) != 0
            or getattr(message, "analog_out_port_num", 0) != 0
        )

    @staticmethod
    def _is_stream_message(message: DaqifiOutMessage) -> bool:
        """A message is a stream message if it has a timestamp + payload data.

        USB firmware sends ``analog_in_data_float`` (pre-scaled floats);
        WiFi firmware may send ``analog_in_data`` (raw integer ADC counts).
        Both must be detected. ``digital_data`` is a ``bytes`` blob.
        """
        if getattr(message, "msg_time_stamp", 0) == 0:
            return False

        analog_in_data = getattr(message, "analog_in_data", ()) or ()
        analog_in_data_float = getattr(message, "analog_in_data_float", ()) or ()
        digital_data = getattr(message, "digital_data", b"") or b""

        return (
            len(analog_in_data) > 0
            or len(analog_in_data_float) > 0
            or len(digital_data) > 0
        )


def _is_daqifi_out_message(value: object) -> bool:
    """Duck-type check for a DaqifiOutMessage-shaped object.

    Accepts anything carrying the ``device_status`` field, which is the
    universal discriminator across status / stream / error variants.
    """
    return value is not None and hasattr(value, "device_status")
