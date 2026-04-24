"""Protocol handlers for DAQiFi device messages.

Ported from upstream ``Daqifi.Core.Device.Protocol.ProtobufProtocolHandler``
(``src/Daqifi.Core/Device/Protocol/ProtobufProtocolHandler.cs`` @ e8d84af).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from daqifi.communication import InboundMessage
from daqifi.enums import ProtobufMessageType

_log = logging.getLogger(__name__)


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

    def _invoke_callback(
        self,
        cb: Callable[[DaqifiOutMessage], None],
        pb_message: DaqifiOutMessage,
    ) -> None:
        # If an integrator hands us an ``async def`` callback by
        # mistake, the coroutine returned here would never be awaited
        # and the body would never run. Log loudly so the misuse
        # surfaces immediately instead of becoming a silent dropped
        # message (Qodo PR 17 pass 35 #1).
        result = cb(pb_message)
        if result is not None and hasattr(result, "__await__"):
            _log.error(
                "Protocol handler callback returned an awaitable; "
                "callbacks must be synchronous (this message was dropped)."
            )

    def can_handle(self, message: InboundMessage[Any]) -> bool:
        """Return True iff ``message.data`` looks like a DaqifiOutMessage.

        Duck-typed: presence of the ``device_status`` attribute is treated as
        the discriminator since the upstream type check (``is DaqifiOutMessage``)
        has no clean Python analog without coupling to a real protobuf class.
        """
        if message is None or not hasattr(message, "data"):
            return False
        return _is_daqifi_out_message(message.data)

    def handle(self, message: InboundMessage[Any]) -> None:
        """Synchronous dispatch — preferred hot-path entry point.

        ``handle_async`` exists for ``IProtocolHandler`` Protocol
        compliance, but in this project the body never awaits, so the
        sync version skips the cost of creating + tearing down an
        event loop per inbound message (Qodo PR 17 finding 4).
        """
        # Mirror `can_handle`'s defensive shape: a malformed event-args
        # arrival (None / no `data` attr) shouldn't crash the consumer
        # thread (Qodo PR 17 pass 2 #4).
        if message is None or not hasattr(message, "data"):
            return
        pb_message = message.data
        if not _is_daqifi_out_message(pb_message):
            return

        # Wrap detection AND dispatch in the try/except — a malformed
        # duck-typed payload could raise from inside detect_message_type
        # (e.g. attribute access on an unexpected type), which would
        # otherwise propagate up the consumer thread (Qodo PR 17 pass
        # 8 #5). `message_type` may be unset if detection itself fails.
        message_type: Optional["ProtobufMessageType"] = None
        try:
            message_type = self.detect_message_type(pb_message)
            if message_type is ProtobufMessageType.STATUS:
                if self._status_message_handler is not None:
                    self._invoke_callback(self._status_message_handler, pb_message)
            elif message_type is ProtobufMessageType.STREAM:
                if self._stream_message_handler is not None:
                    self._invoke_callback(self._stream_message_handler, pb_message)
            elif message_type is ProtobufMessageType.SD_CARD:
                if self._sd_card_message_handler is not None:
                    self._invoke_callback(self._sd_card_message_handler, pb_message)
            elif message_type is ProtobufMessageType.ERROR:
                if self._error_message_handler is not None:
                    self._invoke_callback(self._error_message_handler, pb_message)
            # ProtobufMessageType.UNKNOWN and any unmapped value: no-op.
        except Exception:
            _log.exception(
                "Protocol handler callback raised for %s",
                message_type if message_type is not None else "UNKNOWN",
            )

    async def handle_async(self, message: InboundMessage[Any]) -> None:
        """``IProtocolHandler`` Protocol compliance — delegates to
        the sync :meth:`handle`. Kept as ``async def`` so static
        type checks against the ``IProtocolHandler`` Protocol
        succeed."""
        self.handle(message)

    @staticmethod
    def detect_message_type(message: DaqifiOutMessage) -> ProtobufMessageType:
        """Detect the type of a protobuf message based on its contents."""
        # Check stream FIRST: a streaming frame can incidentally
        # carry the port-count fields _is_status_message looks at,
        # which would otherwise misclassify the frame as STATUS and
        # send it down the wrong handler chain (Qodo PR 17 pass 18 #1).
        if ProtobufProtocolHandler._is_stream_message(message):
            return ProtobufMessageType.STREAM

        # Status messages contain device configuration information
        if ProtobufProtocolHandler._is_status_message(message):
            return ProtobufMessageType.STATUS

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

        # Real protobuf-generated messages always carry sized containers
        # here, but a stub or duck-typed fake might not. Coerce anything
        # without `__len__` to an empty value so the size check below
        # never raises TypeError.
        def _sized_or_empty(value: Any, empty: Any) -> Any:
            if value is None:
                return empty
            return value if hasattr(value, "__len__") else empty

        analog_in_data = _sized_or_empty(getattr(message, "analog_in_data", None), ())
        analog_in_data_float = _sized_or_empty(getattr(message, "analog_in_data_float", None), ())
        digital_data = _sized_or_empty(getattr(message, "digital_data", None), b"")

        return (
            len(analog_in_data) > 0
            or len(analog_in_data_float) > 0
            or len(digital_data) > 0
        )


def _is_daqifi_out_message(value: object) -> bool:
    """Duck-type check for a DaqifiOutMessage-shaped object.

    Accepts anything carrying the ``device_status`` field, which is the
    universal discriminator across status / stream / error variants.
    Uses ``getattr`` inside a ``try/except`` rather than ``hasattr``
    so a property getter that raises a non-AttributeError exception
    can't crash the consumer thread (Qodo PR 17 pass 21 #3).
    """
    if value is None:
        return False
    try:
        getattr(value, "device_status")
    except Exception:
        return False
    return True
