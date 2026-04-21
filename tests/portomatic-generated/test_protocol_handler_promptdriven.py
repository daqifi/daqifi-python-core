"""Tests for daqifi.protocol.ProtobufProtocolHandler (prompt-driven port).

Generated for portomatic ticket #118 head-to-head experiment. Covers handler
dispatch for all four message types, the CanHandle filter, every branch of
DetectMessageType, and the optional-handler-defaults-to-no-op behaviour.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import List

import pytest

from daqifi.communication import InboundMessage
from daqifi.enums import ProtobufMessageType
from daqifi.protocol import ProtobufProtocolHandler


# --- duck-typed stand-in for DaqifiOutMessage --------------------------------


@dataclass
class FakeDaqifiOutMessage:
    """Minimal duck-typed stand-in matching the protobuf field names."""

    device_status: int = 0
    digital_port_num: int = 0
    analog_in_port_num: int = 0
    analog_out_port_num: int = 0
    msg_time_stamp: int = 0
    analog_in_data: List[int] = field(default_factory=list)
    analog_in_data_float: List[float] = field(default_factory=list)
    digital_data: bytes = b""


def _wrap(payload: object) -> InboundMessage[object]:
    return InboundMessage(data=payload)


def _run(coro) -> None:
    asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# --- DetectMessageType branches ---------------------------------------------


def test_detect_status_message_via_digital_port_num() -> None:
    msg = FakeDaqifiOutMessage(digital_port_num=8)
    assert ProtobufProtocolHandler.detect_message_type(msg) is ProtobufMessageType.STATUS


def test_detect_status_message_via_analog_in_port_num() -> None:
    msg = FakeDaqifiOutMessage(analog_in_port_num=4)
    assert ProtobufProtocolHandler.detect_message_type(msg) is ProtobufMessageType.STATUS


def test_detect_stream_message_with_float_data() -> None:
    msg = FakeDaqifiOutMessage(msg_time_stamp=1234, analog_in_data_float=[1.0, 2.0])
    assert ProtobufProtocolHandler.detect_message_type(msg) is ProtobufMessageType.STREAM


def test_detect_stream_message_with_raw_int_data() -> None:
    msg = FakeDaqifiOutMessage(msg_time_stamp=42, analog_in_data=[100, 200, 300])
    assert ProtobufProtocolHandler.detect_message_type(msg) is ProtobufMessageType.STREAM


def test_detect_stream_message_requires_timestamp() -> None:
    # Data present but no timestamp → not a stream message; falls through.
    msg = FakeDaqifiOutMessage(msg_time_stamp=0, analog_in_data_float=[1.0])
    assert ProtobufProtocolHandler.detect_message_type(msg) is ProtobufMessageType.UNKNOWN


def test_detect_error_message() -> None:
    msg = FakeDaqifiOutMessage(device_status=7)
    assert ProtobufProtocolHandler.detect_message_type(msg) is ProtobufMessageType.ERROR


def test_detect_unknown_message() -> None:
    msg = FakeDaqifiOutMessage()  # all-zeros / empty
    assert ProtobufProtocolHandler.detect_message_type(msg) is ProtobufMessageType.UNKNOWN


# --- CanHandle filter --------------------------------------------------------


def test_can_handle_accepts_daqifi_out_message_shape() -> None:
    handler = ProtobufProtocolHandler()
    assert handler.can_handle(_wrap(FakeDaqifiOutMessage())) is True


def test_can_handle_rejects_non_daqifi_payload() -> None:
    handler = ProtobufProtocolHandler()
    assert handler.can_handle(_wrap("not a protobuf message")) is False
    assert handler.can_handle(_wrap(None)) is False
    assert handler.can_handle(_wrap(42)) is False


# --- HandleAsync dispatch (all 4 handlers + no-op defaults) -----------------


def test_handle_async_dispatches_status_message() -> None:
    received: List[FakeDaqifiOutMessage] = []
    handler = ProtobufProtocolHandler(status_message_handler=received.append)
    msg = FakeDaqifiOutMessage(analog_in_port_num=4)

    asyncio.run(handler.handle_async(_wrap(msg)))

    assert received == [msg]


def test_handle_async_dispatches_stream_message() -> None:
    received: List[FakeDaqifiOutMessage] = []
    handler = ProtobufProtocolHandler(stream_message_handler=received.append)
    msg = FakeDaqifiOutMessage(msg_time_stamp=99, analog_in_data_float=[3.14])

    asyncio.run(handler.handle_async(_wrap(msg)))

    assert received == [msg]


def test_handle_async_dispatches_error_message() -> None:
    received: List[FakeDaqifiOutMessage] = []
    handler = ProtobufProtocolHandler(error_message_handler=received.append)
    msg = FakeDaqifiOutMessage(device_status=2)

    asyncio.run(handler.handle_async(_wrap(msg)))

    assert received == [msg]


def test_handle_async_sd_card_branch_invokes_handler_when_routed() -> None:
    """SD card dispatch is wired even though detect() never returns SD_CARD.

    Per the upstream comment, real SD card messages arrive as text not protobuf.
    We exercise the dispatch arm directly to prove it is wired correctly.
    """
    received: List[object] = []
    handler = ProtobufProtocolHandler(sd_card_message_handler=received.append)

    # Synthesise an SD_CARD route by patching detect_message_type for one call.
    msg = FakeDaqifiOutMessage()
    original = ProtobufProtocolHandler.detect_message_type
    try:
        ProtobufProtocolHandler.detect_message_type = staticmethod(  # type: ignore[method-assign]
            lambda _m: ProtobufMessageType.SD_CARD
        )
        asyncio.run(handler.handle_async(_wrap(msg)))
    finally:
        ProtobufProtocolHandler.detect_message_type = staticmethod(original)  # type: ignore[method-assign]

    assert received == [msg]


def test_handle_async_unknown_is_silent_noop() -> None:
    """An UNKNOWN message must not raise even when every handler is set."""
    calls: List[str] = []
    handler = ProtobufProtocolHandler(
        status_message_handler=lambda _m: calls.append("status"),
        stream_message_handler=lambda _m: calls.append("stream"),
        sd_card_message_handler=lambda _m: calls.append("sd"),
        error_message_handler=lambda _m: calls.append("error"),
    )
    asyncio.run(handler.handle_async(_wrap(FakeDaqifiOutMessage())))
    assert calls == []


def test_handle_async_optional_handlers_default_to_noop() -> None:
    """No handlers registered → dispatch must not raise for any message type."""
    handler = ProtobufProtocolHandler()  # all four callbacks default to None

    # Status, stream, error, unknown — all should be silent no-ops.
    asyncio.run(handler.handle_async(_wrap(FakeDaqifiOutMessage(analog_in_port_num=2))))
    asyncio.run(
        handler.handle_async(
            _wrap(FakeDaqifiOutMessage(msg_time_stamp=1, analog_in_data=[1]))
        )
    )
    asyncio.run(handler.handle_async(_wrap(FakeDaqifiOutMessage(device_status=1))))
    asyncio.run(handler.handle_async(_wrap(FakeDaqifiOutMessage())))


def test_handle_async_skips_non_daqifi_payload() -> None:
    """Payload that fails CanHandle must not invoke any registered handler."""
    calls: List[str] = []
    handler = ProtobufProtocolHandler(
        status_message_handler=lambda _m: calls.append("status"),
        stream_message_handler=lambda _m: calls.append("stream"),
        sd_card_message_handler=lambda _m: calls.append("sd"),
        error_message_handler=lambda _m: calls.append("error"),
    )

    asyncio.run(handler.handle_async(_wrap("plain string, not a protobuf")))
    asyncio.run(handler.handle_async(_wrap(None)))

    assert calls == []


def test_handle_async_returns_none() -> None:
    """The coroutine resolves to None (mirrors C# Task.CompletedTask)."""
    handler = ProtobufProtocolHandler()
    result = asyncio.run(handler.handle_async(_wrap(FakeDaqifiOutMessage())))
    assert result is None
