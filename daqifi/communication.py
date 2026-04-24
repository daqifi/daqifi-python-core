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

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import IO, Any, Callable, Generic, List, Optional, Tuple, TypeVar


__all__ = [
    "InboundMessage",
    "TextInboundMessage",
    "ObjectInboundMessage",
    "LineBasedMessageParser",
    "ProtobufMessageParser",
    "CompositeMessageParser",
    "StreamMessageConsumer",
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

    Bad payloads or malformed length prefixes advance one byte and try
    again at the next offset (mirrors upstream resilience). The loop
    is bounded by ``current_index < len(data)``; the caller can re-invoke
    once more bytes arrive.
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

        # NOTE: the original C# port had a `retry_count < _MAX_RETRY_ATTEMPTS`
        # guard here to bail out after consecutive failures. That gate is
        # actively harmful when the buffer contains SCPI text echoes
        # (`DAQIFI> `, `**ERROR ...`, etc.) before any protobuf data:
        # those bytes never parse, the parser bails after 3 retries with
        # only ~3 bytes consumed, and the buffer keeps growing forever
        # since the consumer only drops `consumed` bytes per call. The
        # `current_index < len(data)` condition already bounds the loop —
        # we always advance ≥1 byte on failure — so removing the retry
        # gate keeps termination guaranteed while letting the parser
        # scan past stale text bytes to reach valid frames behind them.
        # Verified 2026-04-23 against NQ1 USB serial streaming.
        while current_index < len(data):
            remaining = data[current_index:]
            message_length, prefix_bytes, is_malformed = _try_read_length_prefix(
                remaining
            )

            if message_length is None:
                if is_malformed:
                    current_index += max(prefix_bytes, 1)
                    consumed_bytes = current_index
                    continue
                break  # need more data for length prefix

            if message_length <= 0 or message_length > _MAX_MESSAGE_SIZE_BYTES:
                current_index += max(prefix_bytes, 1)
                consumed_bytes = current_index
                continue

            if len(remaining) < prefix_bytes + message_length:
                break  # wait for more data

            try:
                payload = remaining[prefix_bytes : prefix_bytes + message_length]
                parsed = self._message_factory(bytes(payload))
                current_index += prefix_bytes + message_length
                consumed_bytes = current_index
                messages.append(ObjectInboundMessage(data=parsed))
            except Exception:
                # Bad payload: advance one byte and try again at the
                # next offset (mirrors upstream resilience pattern).
                current_index += 1
                consumed_bytes = current_index

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

        # Critical: return whenever EITHER messages OR consumed > 0.
        # A parser may legitimately advance through ignorable bytes
        # (blank lines, malformed prefixes) without producing a
        # message; dropping that progress causes the consumer's
        # buffer to grow without bound (Qodo PR 17 pass 6 #1).
        if hint == _MessageTypeHint.LIKELY_PROTOBUF:
            wrapped, consumed = self._try_protobuf(data)
            if wrapped or consumed:
                return wrapped, consumed

        if hint == _MessageTypeHint.LIKELY_TEXT:
            wrapped, consumed = self._try_text(data)
            if wrapped or consumed:
                return wrapped, consumed

        # Heuristics uncertain or first attempt failed -- try the other.
        if hint != _MessageTypeHint.LIKELY_TEXT:
            wrapped, consumed = self._try_text(data)
            if wrapped or consumed:
                return wrapped, consumed

        if hint != _MessageTypeHint.LIKELY_PROTOBUF:
            wrapped, consumed = self._try_protobuf(data)
            if wrapped or consumed:
                return wrapped, consumed

        return [], 0

    def _try_text(self, data: bytes) -> Tuple[List[ObjectInboundMessage], int]:
        text_messages, consumed = self._text_parser.parse_messages(data)
        # Return consumed even when no messages — caller decides whether
        # to drain the buffer (see parse_messages dispatch above).
        wrapped = [ObjectInboundMessage(data=m.data) for m in text_messages]
        return wrapped, consumed

    def _try_protobuf(
        self, data: bytes
    ) -> Tuple[List[ObjectInboundMessage], int]:
        protobuf_messages, consumed = self._protobuf_parser.parse_messages(data)
        # ProtobufMessageParser already wraps in ObjectInboundMessage.
        # Return consumed even when no messages (see _try_text above).
        return list(protobuf_messages), consumed


# ---- StreamMessageConsumer ---------------------------------------------------


@dataclass
class _MessageReceivedArgs(Generic[T]):
    """Local minimal MessageReceivedEventArgs (avoid an import cycle into
    `_internal.comm` and the optional `OutboundMessage` machinery there).

    Same shape as `daqifi._internal.comm.MessageReceivedEventArgs`:
    `message`, `raw_data`, `timestamp` (UTC).
    """

    message: Any
    raw_data: bytes
    timestamp: datetime


@dataclass
class _ConsumerErrorArgs:
    """Same shape as `daqifi._internal.comm.MessageConsumerErrorEventArgs`."""

    error: BaseException
    raw_data: Optional[bytes]
    timestamp: datetime


class StreamMessageConsumer(Generic[T]):
    """Background-thread consumer that pulls bytes off a stream and emits
    parsed messages via callback.

    Ports the C# ``StreamMessageConsumer<T>`` from
    ``Daqifi.Core/Communication/Consumers/StreamMessageConsumer.cs``.
    Satisfies the :class:`daqifi._internal.comm.MessageConsumer` Protocol.

    The reader runs in a daemon :class:`threading.Thread` (mirrors the
    upstream ``Thread { IsBackground = true }``). On every read pass:

    1. Read up to ``buffer_size`` bytes from ``stream``.
    2. Append to an internal byte buffer.
    3. Hand the buffer to ``message_parser.parse_messages(...)``.
    4. Drop ``consumed_bytes`` from the front of the buffer.
    5. Fire ``MessageReceived`` for each parsed message.

    Errors are routed to ``ErrorOccurred`` rather than killing the
    thread, mirroring upstream's resilience.
    """

    def __init__(
        self,
        stream: IO[bytes],
        message_parser: Any,  # Duck-typed: anything with parse_messages(bytes)
        buffer_size: int = 4096,
        is_wifi_device: bool = False,
    ) -> None:
        if stream is None:  # type: ignore[unreachable]
            raise TypeError("stream must not be None.")
        if message_parser is None:  # type: ignore[unreachable]
            raise TypeError("message_parser must not be None.")
        if buffer_size <= 0:
            raise ValueError("buffer_size must be positive.")
        self._stream = stream
        self._parser = message_parser
        self._buffer_size = buffer_size
        self._message_buffer = bytearray()
        self._is_running = False
        self._thread: Optional[threading.Thread] = None
        self._disposed = False
        self.is_wifi_device = is_wifi_device
        self._message_handlers: List[Callable[[Any], None]] = []
        self._error_handlers: List[Callable[[Any], None]] = []
        # Lock around the message buffer mutation so ClearBuffer doesn't
        # race the consumer thread.
        self._buffer_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def queued_message_count(self) -> int:
        with self._buffer_lock:
            return len(self._message_buffer)

    # ---- event subscription ------------------------------------------------

    def add_message_received(self, handler: Callable[[Any], None]) -> None:
        self._message_handlers.append(handler)

    def remove_message_received(self, handler: Callable[[Any], None]) -> None:
        try:
            self._message_handlers.remove(handler)
        except ValueError:
            pass

    def add_error_occurred(self, handler: Callable[[Any], None]) -> None:
        self._error_handlers.append(handler)

    def remove_error_occurred(self, handler: Callable[[Any], None]) -> None:
        try:
            self._error_handlers.remove(handler)
        except ValueError:
            pass

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._raise_if_disposed()
        if self._is_running:
            return
        self._is_running = True
        thread_name = f"MessageConsumer-{type(self).__name__}"
        self._thread = threading.Thread(
            target=self._process_messages,
            name=thread_name,
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._is_running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            # Qodo PR 17 finding 5: only drop the thread reference if
            # it actually stopped. Keeping it lets a caller retry
            # join / inspect is_alive instead of leaking the handle.
            if not self._thread.is_alive():
                self._thread = None
        with self._buffer_lock:
            self._message_buffer.clear()

    def stop_safely(self, timeout_ms: int = 1000) -> bool:
        if not self._is_running:
            return True
        self._is_running = False
        thread = self._thread
        stopped = True
        if thread is not None:
            thread.join(timeout=timeout_ms / 1000.0)
            stopped = not thread.is_alive()
        # Qodo PR 17 finding 5: don't drop the handle if the thread
        # is still alive — caller may need to retry.
        if stopped:
            self._thread = None
        with self._buffer_lock:
            self._message_buffer.clear()
        return stopped

    def clear_buffer(self) -> None:
        self._raise_if_disposed()
        with self._buffer_lock:
            self._message_buffer.clear()
        # Drain stream-level residual data if the stream exposes a
        # peek/in_waiting interface (pyserial: `.in_waiting`; sockets: no
        # standard accessor — skip).
        try:
            in_waiting = getattr(self._stream, "in_waiting", None)
            if in_waiting:
                self._stream.read(in_waiting)
        except Exception:
            pass

    def close(self) -> None:
        if self._disposed:
            return
        self.stop_safely()
        self._disposed = True

    # ---- internals ---------------------------------------------------------

    def _process_messages(self) -> None:
        # `socket.timeout` is the canonical "no data within window"
        # signal when the underlying transport is a TCP socket with a
        # finite read timeout (TcpStreamTransport sets 1.0 s) — treat
        # it as "loop back to check _is_running" rather than as an
        # error to fan out via _on_error. Without this, every 1 s
        # idle window would post a noisy "error" callback (Qodo PR 17
        # finding 5).
        import socket as _socket
        while self._is_running:
            try:
                bytes_read = b""
                try:
                    bytes_read = self._stream.read(self._buffer_size)
                    # Some stream-like objects return None on timeout (pyserial
                    # with non-blocking reads); coerce to empty bytes.
                    if bytes_read is None:
                        bytes_read = b""
                except _socket.timeout:
                    # Expected idle path — no data this window.
                    if not self._is_running:
                        return
                    continue
                except Exception as exc:
                    if not self._is_running:
                        return
                    self._on_error(exc)
                    time.sleep(0.1)  # back off on error
                    continue

                if not bytes_read:
                    time.sleep(0.01)
                    continue

                with self._buffer_lock:
                    self._message_buffer.extend(bytes_read)
                    snapshot = bytes(self._message_buffer)

                try:
                    messages, consumed = self._parser.parse_messages(snapshot)
                except Exception as exc:
                    self._on_error(exc, snapshot)
                    continue

                # Slice the buffer down to only the bytes the parser
                # actually consumed — that's what callers expect as
                # `raw_data`. Passing the full snapshot exposes
                # trailing unparsed bytes that don't belong to any
                # of the produced messages (Qodo PR 17 pass 6 #6).
                consumed_bytes = snapshot[:consumed] if consumed > 0 else b""

                if consumed > 0:
                    with self._buffer_lock:
                        del self._message_buffer[: min(consumed, len(self._message_buffer))]

                for msg in messages:
                    try:
                        self._on_message(msg, consumed_bytes)
                    except Exception as exc:
                        self._on_error(exc, consumed_bytes)
            except Exception as exc:
                if self._is_running:
                    self._on_error(exc)

    def _on_message(self, message: Any, raw_data: bytes) -> None:
        args = _MessageReceivedArgs(
            message=message,
            raw_data=raw_data,
            timestamp=datetime.now(timezone.utc),
        )
        for h in list(self._message_handlers):
            h(args)

    def _on_error(self, error: BaseException, raw_data: Optional[bytes] = None) -> None:
        args = _ConsumerErrorArgs(
            error=error,
            raw_data=raw_data,
            timestamp=datetime.now(timezone.utc),
        )
        for h in list(self._error_handlers):
            try:
                h(args)
            except Exception:
                pass

    def _raise_if_disposed(self) -> None:
        if self._disposed:
            raise RuntimeError("StreamMessageConsumer has been closed.")
