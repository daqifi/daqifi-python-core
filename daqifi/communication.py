"""Message-parsing primitives for line-based device protocols.

Port of `LineBasedMessageParser` and `TextInboundMessage` from
`Daqifi.Core.Communication.Consumers`.

The C# parser implements `IMessageParser<string>` and yields
`IInboundMessage<string>` instances. We keep the same shapes:
`InboundMessage[T]` is a frozen dataclass with a single `data`
field (matches the C# generic interface), and `LineBasedMessageParser`
exposes `parse_messages(data: bytes) -> tuple[list[message], int]`
returning both the parsed messages and the number of bytes consumed.
The C# `out int consumedBytes` parameter becomes the second tuple
element since Python doesn't have out-params.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, List, Optional, Tuple, TypeVar


__all__ = [
    "InboundMessage",
    "TextInboundMessage",
    "ObjectInboundMessage",
    "LineBasedMessageParser",
    "ProtobufMessageParser",
    "CompositeMessageParser",
]


T = TypeVar("T")


@dataclass(frozen=True)
class InboundMessage(Generic[T]):
    """Parsed inbound message — the Python equivalent of `IInboundMessage<T>`."""

    data: T


# Concrete alias matching the C# `TextInboundMessage` name. Same shape
# as `InboundMessage[str]` — kept as its own type for documentation
# clarity and parity with C# call sites.
TextInboundMessage = InboundMessage[str]


@dataclass(frozen=True)
class ObjectInboundMessage:
    """Parsed inbound message carrying arbitrary object data.

    Ports the C# ``ObjectInboundMessage`` (defined alongside
    :class:`CompositeMessageParser` in
    ``Daqifi.Core/Communication/Consumers/CompositeMessageParser.cs``).
    Same ``data`` shape as :class:`InboundMessage[object]` -- kept as its
    own concrete class for documentation clarity and to match upstream
    call sites (mirrors the :data:`TextInboundMessage` aliasing pattern).
    """

    data: object


class LineBasedMessageParser:
    """Splits a stream of bytes into text messages on a line-ending delimiter.

    Default line ending is CRLF and default encoding is UTF-8 — matches
    SCPI / DAQiFi text-protocol responses.
    """

    def __init__(self, line_ending: str = "\r\n", encoding: str = "utf-8") -> None:
        if not line_ending:
            # An empty line-ending would make `bytes.find` return 0 on every
            # iteration, putting `parse_messages` into an infinite loop.
            raise ValueError("line_ending must not be empty")
        self._encoding = encoding
        self._line_ending = line_ending.encode(encoding)

    def parse_messages(self, data: bytes) -> Tuple[List[InboundMessage[str]], int]:
        """Parse `data` into a list of text messages plus consumed-byte count.

        Returns:
            (messages, consumed_bytes) — `messages` only contains complete
            lines; trailing partial data leaves `consumed_bytes` < `len(data)`
            so the caller can keep accumulating.
        """
        messages: List[InboundMessage[str]] = []
        consumed_bytes = 0

        if len(data) == 0:
            return messages, consumed_bytes

        search_start = 0
        line_ending_len = len(self._line_ending)

        while search_start < len(data):
            line_end_index = data.find(self._line_ending, search_start)
            if line_end_index == -1:
                break

            line_length = line_end_index - search_start
            if line_length > 0:
                line_data = data[search_start:line_end_index]
                # Use errors="replace" so a malformed byte sequence in the
                # middle of a serial stream doesn't take down the whole
                # parse — surface the partial text rather than raising.
                message_text = line_data.decode(self._encoding, errors="replace")
                stripped = message_text.strip()
                if stripped:
                    messages.append(InboundMessage(data=stripped))

            search_start = line_end_index + line_ending_len
            consumed_bytes = search_start

        return messages, consumed_bytes


# ---- Protobuf framing constants (mirror upstream limits) ----
_MAX_RETRY_ATTEMPTS = 3
_MAX_VARINT32_BYTES = 5
_MAX_MESSAGE_SIZE_BYTES = 1024 * 1024


def _try_read_length_prefix(data: bytes) -> Tuple[Optional[int], int, bool]:
    """Decode a protobuf-style varint length prefix.

    Returns ``(length, bytes_read, is_malformed)``. ``length`` is ``None``
    when the prefix is incomplete (more bytes needed) or malformed. Mirrors
    the upstream ``TryReadLengthPrefix`` helper.
    """
    length = 0
    bytes_read = 0
    shift = 0
    for i in range(_MAX_VARINT32_BYTES):
        if i >= len(data):
            return None, bytes_read, False  # incomplete
        value = data[i]
        length |= (value & 0x7F) << shift
        bytes_read += 1
        if (value & 0x80) == 0:
            return length, bytes_read, False
        shift += 7
    return None, bytes_read, True  # malformed (varint too long)


class ProtobufMessageParser:
    """Length-prefixed protobuf message parser.

    Ports the C# ``ProtobufMessageParser`` from
    ``Daqifi.Core/Communication/Consumers/ProtobufMessageParser.cs``.
    Reads a varint length prefix, slices the payload, and hands it to a
    caller-supplied factory function to build the message object.

    The upstream class hard-codes ``DaqifiOutMessage.Parser.ParseFrom``;
    the Python port takes the factory as a constructor argument because
    ``DaqifiOutMessage`` is duck-typed in this project (see
    :mod:`daqifi.protocol`). Pass the generated protobuf class's
    ``FromString`` (e.g. ``DaqifiOutMessage.FromString``) or any callable
    that turns ``bytes`` into the message object.

    Bad payloads or malformed length prefixes advance one byte and retry,
    matching the upstream's resilience behaviour. After
    :data:`_MAX_RETRY_ATTEMPTS` consecutive failures the parser stops and
    returns whatever it has so far -- the caller can re-invoke once more
    bytes arrive.
    """

    def __init__(self, message_factory: Callable[[bytes], object]) -> None:
        self._message_factory = message_factory

    def parse_messages(
        self, data: bytes
    ) -> Tuple[List[ObjectInboundMessage], int]:
        messages: List[ObjectInboundMessage] = []
        consumed_bytes = 0
        if len(data) == 0:
            return messages, consumed_bytes

        current_index = 0
        retry_count = 0

        while current_index < len(data) and retry_count < _MAX_RETRY_ATTEMPTS:
            remaining = data[current_index:]
            message_length, prefix_bytes, is_malformed = _try_read_length_prefix(
                remaining
            )

            if message_length is None:
                if is_malformed:
                    current_index += max(prefix_bytes, 1)
                    consumed_bytes = current_index
                    retry_count += 1
                    continue
                break  # need more data for length prefix

            if message_length <= 0 or message_length > _MAX_MESSAGE_SIZE_BYTES:
                current_index += max(prefix_bytes, 1)
                consumed_bytes = current_index
                retry_count += 1
                continue

            if len(remaining) < prefix_bytes + message_length:
                break  # wait for more data

            try:
                payload = remaining[prefix_bytes : prefix_bytes + message_length]
                parsed = self._message_factory(bytes(payload))
                current_index += prefix_bytes + message_length
                consumed_bytes = current_index
                messages.append(ObjectInboundMessage(data=parsed))
                retry_count = 0
            except Exception:
                # Bad payload: advance one byte and retry, mirroring upstream.
                current_index += 1
                consumed_bytes = current_index
                retry_count += 1

        return messages, consumed_bytes


class _MessageTypeHint:
    UNCERTAIN = 0
    LIKELY_TEXT = 1
    LIKELY_PROTOBUF = 2


def _detect_message_type(data: bytes) -> int:
    """Heuristic: choose between text and protobuf parsing for ``data``."""
    if len(data) == 0:
        return _MessageTypeHint.UNCERTAIN

    printable = sum(1 for b in data if 32 <= b <= 126)
    if printable / len(data) > 0.8:
        return _MessageTypeHint.LIKELY_TEXT

    if len(data) > 3 and _is_likely_text_command(data):
        return _MessageTypeHint.LIKELY_TEXT

    null_ratio = sum(1 for b in data if b == 0) / len(data)
    if null_ratio > 0.1:
        return _MessageTypeHint.LIKELY_PROTOBUF

    if null_ratio > 0.05 and _is_likely_protobuf_data(data):
        return _MessageTypeHint.LIKELY_PROTOBUF

    return _MessageTypeHint.UNCERTAIN


def _is_likely_text_command(data: bytes) -> bool:
    head = data[:10].decode("ascii", errors="ignore")
    full = data.decode("ascii", errors="ignore")
    head_upper = head.upper()
    return (
        head.startswith("*")
        or head.startswith("SYST")
        or head.startswith("CONF")
        or head_upper.startswith("READ")  # case-insensitive per upstream
        or full.endswith("\r\n")
        or full.endswith("\n")
    )


def _is_likely_protobuf_data(data: bytes) -> bool:
    if len(data) < 2:
        return False
    # Look for a plausible varint field tag in the first few bytes:
    # wire type (low 3 bits) <= 5 and field number (high bits) > 0.
    for i in range(min(len(data) - 1, 5)):
        b = data[i]
        if (b & 0x07) <= 5 and (b >> 3) > 0:
            return True
    return False


class CompositeMessageParser:
    """Parser that auto-routes between text (SCPI) and binary (protobuf).

    Ports the C# ``CompositeMessageParser`` from
    ``Daqifi.Core/Communication/Consumers/CompositeMessageParser.cs``.
    When neither parser is supplied, defaults are constructed:
    :class:`LineBasedMessageParser` for text and a
    :class:`ProtobufMessageParser` whose ``message_factory`` is the
    caller-supplied ``protobuf_factory`` (defaulting to a no-op factory
    that returns the raw bytes -- the upstream pre-wires
    ``ProtobufMessageParser`` to ``DaqifiOutMessage``; we can't, since
    the project treats ``DaqifiOutMessage`` as duck-typed).

    Output is always wrapped in :class:`ObjectInboundMessage`, matching
    the upstream's ``IInboundMessage<object>`` shape.
    """

    def __init__(
        self,
        text_parser: Optional[LineBasedMessageParser] = None,
        protobuf_parser: Optional[ProtobufMessageParser] = None,
        protobuf_factory: Optional[Callable[[bytes], object]] = None,
    ) -> None:
        self._text_parser = text_parser or LineBasedMessageParser()
        if protobuf_parser is not None:
            self._protobuf_parser = protobuf_parser
        else:
            factory = protobuf_factory or (lambda b: b)
            self._protobuf_parser = ProtobufMessageParser(factory)

    def parse_messages(
        self, data: bytes
    ) -> Tuple[List[ObjectInboundMessage], int]:
        if len(data) == 0:
            return [], 0

        hint = _detect_message_type(data)

        if hint == _MessageTypeHint.LIKELY_PROTOBUF:
            wrapped, consumed = self._try_protobuf(data)
            if wrapped:
                return wrapped, consumed

        if hint == _MessageTypeHint.LIKELY_TEXT:
            wrapped, consumed = self._try_text(data)
            if wrapped:
                return wrapped, consumed

        # Heuristics uncertain or first attempt failed -- try the other.
        if hint != _MessageTypeHint.LIKELY_TEXT:
            wrapped, consumed = self._try_text(data)
            if wrapped:
                return wrapped, consumed

        if hint != _MessageTypeHint.LIKELY_PROTOBUF:
            wrapped, consumed = self._try_protobuf(data)
            if wrapped:
                return wrapped, consumed

        return [], 0

    def _try_text(self, data: bytes) -> Tuple[List[ObjectInboundMessage], int]:
        text_messages, consumed = self._text_parser.parse_messages(data)
        if not text_messages:
            return [], 0
        wrapped = [ObjectInboundMessage(data=m.data) for m in text_messages]
        return wrapped, consumed

    def _try_protobuf(
        self, data: bytes
    ) -> Tuple[List[ObjectInboundMessage], int]:
        protobuf_messages, consumed = self._protobuf_parser.parse_messages(data)
        if not protobuf_messages:
            return [], 0
        # ProtobufMessageParser already wraps in ObjectInboundMessage.
        return list(protobuf_messages), consumed
