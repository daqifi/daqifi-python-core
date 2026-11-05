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

import logging
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
    "StreamConsumerTelemetry",
    "StreamMessageConsumer",
]


T = TypeVar("T")


_log = logging.getLogger(__name__)

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
    the consumer's parser is chosen by the caller for the mode the device
    is in; there is no content-guessing router (#231).
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

    def __init__(
        self,
        line_ending: str = "\r\n",
        encoding: str = "utf-8",
        emit_empty_lines: bool = False,
    ) -> None:
        """``line_ending="\n"`` reads LF- and CRLF-terminated lines alike
        (the CR is stripped with the line). ``emit_empty_lines`` emits a
        complete line with no content as an empty message instead of
        dropping it: the text exchange counts a lone CRLF as an answer
        (upstream a57a86a)."""
        if not line_ending:
            # An empty line-ending would make `bytes.find` return 0 on every
            # iteration, putting `parse_messages` into an infinite loop.
            raise ValueError("line_ending must not be empty")
        self._encoding = encoding
        self._line_ending = line_ending.encode(encoding)
        self._emit_empty_lines = emit_empty_lines

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
            cleaned = ""
            if line_length > 0:
                line_data = data[search_start:line_end_index]
                # Use errors="replace" so a malformed byte sequence in the
                # middle of a serial stream doesn't take down the whole
                # parse — surface the partial text rather than raising.
                message_text = line_data.decode(self._encoding, errors="replace")
                # rstrip only the trailing CR/LF so leading/trailing
                # whitespace inside a SCPI response (e.g. \"  X1234\")
                # isn't silently mangled (Qodo PR 17 pass 46 #1).
                cleaned = message_text.rstrip("\r\n")
            if cleaned or self._emit_empty_lines:
                messages.append(InboundMessage(data=cleaned))

            search_start = line_end_index + line_ending_len
            consumed_bytes = search_start

        return messages, consumed_bytes


# ---- Protobuf framing constants (mirror upstream limits) ----
_MAX_VARINT32_BYTES = 5

# Upper bound on a single DaqifiOutMessage frame. Real streaming frames
# are well under 1 KB even at 32 channels (16 analog floats + 16 digital
# bits + headers); initial-status frames that include channel metadata
# and calibration are a few KB at most. 4 KB leaves generous headroom
# for any legitimate frame while cutting off the failure mode where
# boot-time garbage on the serial port happens to form a plausible
# varint prefix encoding tens of KB — previously accepted under the old
# 1 MB cap, which left the parser waiting indefinitely for data that
# would never arrive (device LED streaming, but zero frames parsed; the
# buffer grows unbounded). Declared lengths over this cap are rejected
# immediately and the parser resyncs one byte at a time.
_MAX_MESSAGE_SIZE_BYTES = 4 * 1024

# Maximum gap between the declared message length and the bytes
# currently available before we give up waiting and treat the prefix
# as garbage. A legitimate partial frame is always small — the stream
# consumer feeds parse_messages every few milliseconds and real frames
# are <1 KB, so a real partial never leaves a multi-KB gap between
# declared and available bytes. A prefix that *claims* thousands of
# bytes and is nowhere close to being satisfied is overwhelmingly
# likely to be boot-time garbage (USB CDC setup, DTR pulse bytes) that
# happens to varint-encode a large value. Capping the tolerable gap
# bounds the worst-case stall: without this, a plausible-under-the-cap
# bogus prefix still deadlocks until the buffer reaches the declared
# size, which at slow sample rates can be many seconds.
_MAX_PARTIAL_FRAME_GAP_BYTES = 1024


def _is_plausible_field_tag_byte(b: int) -> bool:
    """Return True if ``b`` could plausibly be the first byte of a real
    DaqifiOutMessage body (a protobuf field tag).

    Used by the partial-frame wait path to reject garbage whose varint
    prefix happens to decode to a plausible-looking length.

    Mirrors upstream ``IsPlausibleFieldTagByte`` from daqifi-core PR #173,
    plus a tightening (Qodo /agentic_review on PR #100): validate the
    wire-type bits even when the continuation bit is set, since wire_type
    lives in the low 3 bits of the first byte regardless of whether the
    field number spans multiple bytes.
    """
    # Wire type lives in the low 3 bits regardless of the continuation
    # bit. Reject impossible wire types even on multi-byte tags. Valid
    # wire types are VARINT(0), I64(1), LEN(2), I32(5); 3 (SGROUP) and
    # 4 (EGROUP) are deprecated group types that DaqifiOutMessage does
    # not use; 6/7 are undefined.
    wire_type = b & 0x07
    if wire_type in (3, 4, 6, 7):
        return False

    # Continuation bit set → multi-byte tag. The full field number
    # spans more bytes (which we may not have yet); we can't validate
    # field_number from this single byte alone, so accept once wire
    # type passed.
    if (b & 0x80) != 0:
        return True

    # Single-byte tag: field number is bits 3-6 of the first byte.
    # Field number 0 is reserved and never appears in a real message.
    field_number = (b >> 3) & 0x0F
    return field_number != 0


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

    #: Console lines that are noise to a frame parser. Matched ONLY as a
    #: complete line at a frame boundary -- see _take_console_noise.
    _CONSOLE_PROMPT = b"DAQIFI> "
    _CONSOLE_ERROR_PREFIX = b"**ERROR: "
    #: Unmatched echoes past which something is wrong and a log says so.
    #: NOT a cap: evicting an unconsumed echo stalls its reply.
    _ECHO_BACKLOG_WARN_AT = 16

    def __init__(
        self,
        message_factory: Callable[[bytes], object],
        console_error_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._message_factory = message_factory
        # Where a ``**ERROR:`` line goes. It is noise to this parser but it
        # is the device reporting something, so it is handed on rather than
        # dropped.
        self._console_error_sink = console_error_sink
        # Commands the library has sent and may see echoed back. Keyed on
        # what was ACTUALLY sent: #204's audit rounds showed that no
        # byte-level "does this look like a command" rule works, because on
        # a length-prefixed protocol a printable byte is also a valid
        # length or field tag.
        self._expected_echoes: List[bytes] = []
        # Guards _expected_echoes. send() appends from the caller's thread
        # while the consumer matches and removes from its own.
        self._echo_lock = threading.Lock()

    def expect_console_echo(self, sent: bytes) -> None:
        """Note the EXACT bytes just written, so an echo of them at a frame
        boundary can be recognised.

        Takes the bytes as sent, not a command string: an earlier version
        took a string and ``strip()``ed it, which discarded whitespace that
        ``get_bytes()`` really does put on the wire (a trailing space), so
        the echo never matched and the stall it exists to prevent came
        back. Only the trailing line ending is removed, because that is
        matched separately -- the device may echo CRLF or bare LF.
        """
        if isinstance(sent, str):            # tolerated, but discouraged
            sent = sent.encode("ascii", errors="ignore")
        line = bytes(sent).rstrip(b"\r\n")
        if not line:
            return
        with self._echo_lock:
            self._expected_echoes.append(line)
            # ⛔ NO SILENT EVICTION. Dropping the oldest unconsumed echo
            # stalled the reply it belonged to -- nine sends before the
            # consumer ran left the ninth reply waiting forever. The list
            # is unbounded and entries leave only when matched; a log says
            # so if it grows past what a healthy exchange needs.
            if len(self._expected_echoes) == self._ECHO_BACKLOG_WARN_AT:
                _log.warning(
                    "%d command echoes are still unmatched; the device may "
                    "not be echoing, or replies are not being read",
                    len(self._expected_echoes),
                )

    def _take_matching_echo(self, remaining: bytes) -> int:
        """Length of a complete expected echo at the start of ``remaining``,
        removing it; 0 if none.

        Match and removal happen under ONE lock hold. Separately they
        raced: ``send()`` could evict between the consumer's match and its
        remove, ``remove()`` raised ValueError, and the consumer then
        discarded a byte of real data.
        """
        with self._echo_lock:
            for echo in self._expected_echoes:
                for ending in (b"\r\n", b"\n"):
                    if remaining.startswith(echo + ending):
                        self._expected_echoes.remove(echo)
                        return len(echo) + len(ending)
        return 0

    def _console_prefix_pending(self, remaining: bytes) -> bool:
        """Whether ``remaining`` is a STRICT PREFIX of console noise, so the
        rest may still be arriving.

        ⛔ Without this a split console line was handed to the frame
        parser, which discarded a byte of it: an echo arriving as
        ``b'EN' | b'NAble...'`` lost the ``E`` and then stalled the next
        reply forever. Holding a partial is the same rule the frame parser
        already applies to a partial frame.
        """
        if not remaining:
            return True
        if self._CONSOLE_PROMPT.startswith(remaining):
            return True
        if self._CONSOLE_ERROR_PREFIX.startswith(remaining):
            return True
        if (remaining.startswith(self._CONSOLE_ERROR_PREFIX)
                and b"\n" not in remaining):
            return True            # a complete prefix, line still arriving
        with self._echo_lock:
            for echo in self._expected_echoes:
                full = echo + b"\r\n"
                if full.startswith(remaining) and len(remaining) < len(full):
                    return True
        return False

    def _take_console_error(self, remaining: bytes) -> int:
        if not remaining.startswith(self._CONSOLE_ERROR_PREFIX):
            return 0
        end = remaining.find(b"\n")
        if end == -1:
            return 0                          # handled by the prefix rule
        line = remaining[:end].rstrip(b"\r").decode("ascii", errors="replace")
        if self._console_error_sink is not None:
            try:
                self._console_error_sink(line)
            except Exception:
                pass
        return end + 1

    def _take_console_noise(self, remaining: bytes) -> int:
        """Complete console noise at the start of ``remaining``, or 0.

        ⛔ THE FRAME INTERPRETATION WINS WHEREVER IT CAN BE DECIDED, so
        this is consulted only after the frame reading has been REFUTED or
        found incomplete -- never before. On a length-prefixed stream the
        two readings genuinely overlap: ``44 41 51 49 46 49 3E 20`` is both
        the prompt ``"DAQIFI> "`` and a 68-byte frame whose body starts
        ``AQIFI> ``. Something has to win, and the frame does, because a
        frame that DECODES is positive evidence while "these bytes spell a
        prompt" is a coincidence that a 69-byte payload can always produce.

        ⚠ RESIDUAL, stated rather than hidden: a frame is preferred only
        once enough bytes are present to decode it. A real frame whose
        length is exactly 68 and whose body begins ``AQIFI> ``, arriving
        split so that only those first 8 bytes are buffered at a boundary,
        is read as the prompt and loses 8 bytes. That needs the length byte
        to be exactly 0x44 AND the next seven payload bytes to be exactly
        ``AQIFI> `` AND the read to split there. The alternative -- never
        accepting the prompt until more bytes arrive -- reintroduces the
        stall this whole change exists to remove, so the trade is
        deliberate.
        """
        n = self._take_matching_echo(remaining)
        if n:
            return n
        if remaining.startswith(self._CONSOLE_PROMPT):
            return len(self._CONSOLE_PROMPT)
        return self._take_console_error(remaining)

    def parse_messages(
        self, data: bytes
    ) -> Tuple[List[ObjectInboundMessage], int]:
        messages: List[ObjectInboundMessage] = []
        consumed_bytes = 0
        if len(data) == 0:
            return messages, consumed_bytes

        current_index = 0

        # The C# upstream originally had a `retry_count < MaxRetryAttempts`
        # guard that bailed after 3 failures (removed in upstream PR #169).
        # That gate is actively harmful when the buffer contains SCPI text
        # echoes (`DAQIFI> `, `**ERROR ...`, etc.) before any protobuf
        # data: those bytes never parse, the parser bails after 3 retries
        # with only ~3 bytes consumed, and the buffer keeps growing
        # forever since the consumer only drops `consumed` bytes per call.
        # The `current_index < len(data)` condition already bounds the
        # loop — we always advance ≥1 byte on failure.
        while current_index < len(data):
            remaining = data[current_index:]
            # ⛔ FRAME FIRST. The console check used to run here, before the
            # length prefix was even read, and it could therefore eat a
            # real frame whose first bytes happen to spell console text.
            # It now runs only where the frame reading has been REFUTED or
            # found incomplete -- see _take_console_noise for why the frame
            # wins and what residual that leaves.
            message_length, prefix_bytes, is_malformed = _try_read_length_prefix(
                remaining
            )

            if message_length is None:
                if is_malformed:
                    # Not a frame at all: now console may be considered.
                    noise = self._take_console_noise(remaining)
                    if noise:
                        current_index += noise
                        consumed_bytes = current_index
                        continue
                    if self._console_prefix_pending(remaining):
                        break
                    # Advance by 1 (not by prefix_bytes) — the malformed
                    # run may overlap a valid frame that starts mid-way
                    # through it. Skipping the whole malformed varint
                    # would lose that frame. Mirrors upstream PR #169.
                    current_index += 1
                    consumed_bytes = current_index
                    continue
                # Not enough bytes even for a length prefix. A complete
                # console token decides it; a partial one waits.
                noise = self._take_console_noise(remaining)
                if noise:
                    current_index += noise
                    consumed_bytes = current_index
                    continue
                break  # need more data for length prefix

            if message_length <= 0 or message_length > _MAX_MESSAGE_SIZE_BYTES:
                # The frame reading is refuted, so console may decide.
                noise = self._take_console_noise(remaining)
                if noise:
                    current_index += noise
                    consumed_bytes = current_index
                    continue
                if self._console_prefix_pending(remaining):
                    break
                # Same single-byte advance: the bytes that decoded to
                # this implausible length may include the start of a
                # valid frame.
                current_index += 1
                consumed_bytes = current_index
                continue

            if len(remaining) < prefix_bytes + message_length:
                # Not enough buffered data for this frame yet. For a real
                # partial frame this just means "wait a bit" — streaming
                # messages are well under 1 KB, so the next read or two
                # will complete it. But a plausible-looking yet bogus
                # prefix can claim thousands of bytes, and waiting for
                # that much never-arriving data is exactly the stall
                # that masquerades as "parser hung": device LED blinking,
                # buffer growing, zero messages parsed.
                #
                # Two recovery gates protect the wait path from garbage
                # while leaving real partial frames intact (mirrors
                # upstream PR #173):
                #
                #  1. Gap gate: a declared length more than
                #     `_MAX_PARTIAL_FRAME_GAP_BYTES` beyond the payload
                #     bytes currently buffered is almost certainly
                #     garbage. Real partials for streaming frames leave
                #     gaps measured in hundreds of bytes, not kilobytes.
                #
                #  2. Field-tag gate: when at least one body byte is
                #     available, peek at it. The first body byte of a
                #     real DaqifiOutMessage is a protobuf field tag —
                #     field_number >= 1 and wire_type in {0,1,2,5}. A
                #     byte that fails both conditions is not a real
                #     frame start; treat it as garbage. A multi-byte
                #     tag (continuation bit set) can't be fully
                #     validated from one byte, so we let it through and
                #     wait.
                #
                # Neither gate advances into a real frame's prefix or
                # payload: only into bytes already identified as
                # implausible.
                available_body_bytes = len(remaining) - prefix_bytes
                missing_payload = message_length - available_body_bytes
                first_body_byte_is_garbage = (
                    available_body_bytes > 0
                    and not _is_plausible_field_tag_byte(remaining[prefix_bytes])
                )

                # Apply the gap heuristic only when we have NO body bytes
                # to validate yet. As soon as one body byte is available,
                # the field-tag gate can decide on its own — gating on
                # missing_payload at that point would corrupt legitimate
                # large frames whose first chunk happens to land right
                # at the prefix+1 boundary. (Qodo /agentic_review on PR
                # #100 caught the original >1024 form; pass 3 /improve
                # tightened from `<= 1` to `== 0` to handle the edge
                # case at exactly one body byte. Same bug exists in
                # upstream daqifi-core PR #173.)
                gap_is_suspicious = (
                    available_body_bytes == 0
                    and missing_payload > _MAX_PARTIAL_FRAME_GAP_BYTES
                )

                if gap_is_suspicious or first_body_byte_is_garbage:
                    noise = self._take_console_noise(remaining)
                    if noise:
                        current_index += noise
                        consumed_bytes = current_index
                        continue
                    if self._console_prefix_pending(remaining):
                        break
                    current_index += 1
                    consumed_bytes = current_index
                    continue

                # A plausible partial frame. A COMPLETE console token still
                # wins here -- this is the residual documented on
                # _take_console_noise -- because refusing it is what made
                # the prompt stall the stream.
                noise = self._take_console_noise(remaining)
                if noise:
                    current_index += noise
                    consumed_bytes = current_index
                    continue
                break  # legitimate partial frame — wait for more data

            try:
                payload = remaining[prefix_bytes : prefix_bytes + message_length]
                parsed = self._message_factory(bytes(payload))
                current_index += prefix_bytes + message_length
                consumed_bytes = current_index
                messages.append(ObjectInboundMessage(data=parsed))
            except Exception:
                # The frame reading is refuted by the decode itself: these
                # bytes had a plausible prefix and enough of them, and are
                # still not a message. NOW console may decide, which is how
                # "DAQIFI> " followed by a real frame resolves -- the
                # 68-byte body starting "AQIFI> " does not decode.
                noise = self._take_console_noise(remaining)
                if noise:
                    current_index += noise
                    consumed_bytes = current_index
                    continue
                # Bad payload: advance one byte and try again at the
                # next offset (mirrors upstream resilience pattern).
                current_index += 1
                consumed_bytes = current_index

        return messages, consumed_bytes


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


@dataclass
class StreamConsumerTelemetry:
    """Lightweight performance counters for :class:`StreamMessageConsumer`.

    Closes #18 §3 — gives operators visibility into what the consumer
    thread is actually doing without attaching a profiler. All counters
    are running totals from consumer start; reset by ``start()``.

    Counters:
    * ``read_calls``       successful ``stream.read()`` calls
    * ``read_bytes_total`` bytes returned by those reads (sum)
    * ``read_bytes_max``   largest single ``stream.read()`` payload
    * ``read_seconds``     wall-clock spent in ``stream.read()``
    * ``parse_calls``      ``parser.parse_messages()`` invocations
    * ``parse_seconds``    wall-clock spent in ``parser.parse_messages()``
    * ``messages_emitted`` total messages handed to ``MessageReceived``
      callbacks (after parse, regardless of decode success)
    """

    read_calls: int = 0
    read_bytes_total: int = 0
    read_bytes_max: int = 0
    read_seconds: float = 0.0
    parse_calls: int = 0
    parse_seconds: float = 0.0
    messages_emitted: int = 0

    # Idle-window counter — bumped each time the read loop spins up
    # but ``stream.read()`` returned no data. A consistently-rising
    # value while a stream is supposed to be flowing means the device
    # has gone quiet (likely a firmware-side stop or a USB CDC pause).
    # Closes the drop-detection requirement gap from Qodo PR #68
    # review #1: msg_time_stamp deltas are unreliable on fw 3.4.6b1
    # (timestamp_freq=0 ⇒ the per-frame counter is disabled), so this
    # transport-level signal is the portable observability primitive.
    idle_reads: int = 0

    @property
    def read_bytes_avg(self) -> float:
        """Average payload per ``stream.read()``. ``0.0`` when no reads yet."""
        return self.read_bytes_total / self.read_calls if self.read_calls else 0.0


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

    # Match SerialStreamTransport.DEFAULT_OS_RX_BUFFER_BYTES so a
    # single `stream.read()` call empties the OS-level USB CDC buffer
    # in one syscall when it's full. Reading in smaller chunks would
    # need multiple syscalls per drain — tolerable, but adds context-
    # switch overhead and slightly raises the chance of an OS-level
    # overflow if the parse path stalls between reads.
    DEFAULT_BUFFER_SIZE = 64 * 1024

    def __init__(
        self,
        stream: IO[bytes],
        message_parser: Any,  # Duck-typed: anything with parse_messages(bytes)
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        is_wifi_device: bool = False,
        health_sink: Optional[Any] = None,
        read_available: bool = False,
    ) -> None:
        """``read_available``: read only what the stream reports waiting
        (``in_waiting``), or one byte when nothing is, instead of
        ``buffer_size`` bytes. A serial port's ``read(n)`` returns early only
        once ``n`` bytes arrive, so a short reply otherwise lands only after
        the port's whole read timeout. For the text exchange; a stream
        without ``in_waiting`` is read as before."""
        if stream is None:  # type: ignore[unreachable]
            raise TypeError("stream must not be None.")
        if message_parser is None:  # type: ignore[unreachable]
            raise TypeError("message_parser must not be None.")
        if buffer_size <= 0:
            raise ValueError("buffer_size must be positive.")
        self._stream = stream
        self._parser = message_parser
        self._buffer_size = buffer_size
        self._read_available = read_available
        # Optional transport told how each read went (report_io_fault /
        # report_io_success), so it can notice a device that went away
        # (#191). A timeout is neither.
        self._health_sink = health_sink
        self._message_buffer = bytearray()
        # Hard cap to prevent unbounded growth if the parser
        # consistently fails to consume bytes (e.g. fed garbage
        # forever). When the buffer crosses this size, drop the
        # oldest half — keeps memory bounded while preserving the
        # most recent (most likely to be a valid frame head) bytes
        # (Qodo PR 17 pass 17 #2).
        self._max_buffer_bytes = 4 * 1024 * 1024  # 4 MiB
        self._is_running = False
        self._thread: Optional[threading.Thread] = None
        self._disposed = False
        self.is_wifi_device = is_wifi_device
        self._message_handlers: List[Callable[[Any], None]] = []
        self._error_handlers: List[Callable[[Any], None]] = []
        # Lock around the message buffer mutation so ClearBuffer doesn't
        # race the consumer thread.
        self._buffer_lock = threading.Lock()
        # Telemetry — closes #18 §3. Mutated only from the consumer
        # thread (read in ``_process_messages``); read from any thread
        # via the ``telemetry`` property. The dataclass holds plain
        # numbers so concurrent reads see a torn-but-monotonic snapshot
        # rather than locking the hot path.
        self._telemetry = StreamConsumerTelemetry()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def queued_message_count(self) -> int:
        with self._buffer_lock:
            return len(self._message_buffer)

    @property
    def telemetry(self) -> StreamConsumerTelemetry:
        """Snapshot of consumer-thread performance counters (#18 §3).

        Returns the live telemetry dataclass — counters keep advancing
        while the consumer is running. Reset by ``start()``.
        """
        return self._telemetry

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
        # Refuse to start if a previous thread didn't fully stop —
        # otherwise we'd race two daemon threads on the same stream
        # (Qodo PR #68 review #3). The 1.0s join in ``stop()`` can
        # time out if the underlying ``stream.read()`` is blocked;
        # caller should ``stop_safely(timeout_ms=...)`` with a longer
        # window or close the stream before retrying.
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(
                "previous consumer thread is still alive; "
                "call stop_safely() with a larger timeout or close "
                "the underlying stream before restarting"
            )
        # Reset telemetry so each start/stop cycle reports its own
        # counters (#18 §3).
        self._telemetry = StreamConsumerTelemetry()
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
            # Don't self-join — `thread.join()` from inside the
            # thread it would join raises RuntimeError. This can
            # happen if a user callback ends up calling stop() (Qodo
            # PR 17 pass 10 #2).
            if threading.current_thread() is not self._thread:
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
            if threading.current_thread() is thread:
                # Self-stop from inside a callback: report
                # "couldn't stop synchronously" instead of
                # raising on the join (Qodo PR 17 pass 10 #2).
                stopped = False
            else:
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

    def _read_size(self) -> int:
        if not self._read_available:
            return self._buffer_size
        try:
            waiting = self._stream.in_waiting
        except AttributeError:
            return self._buffer_size
        except Exception:
            # Let the read itself report a broken stream.
            return 1
        return max(1, min(int(waiting or 0), self._buffer_size))

    def _process_messages(self) -> None:
        # `socket.timeout` is the canonical "no data within window"
        # signal when the underlying transport is a TCP socket with a
        # finite read timeout (TcpStreamTransport sets 1.0 s) — treat
        # it as "loop back to check _is_running" rather than as an
        # error to fan out via _on_error. Without this, every 1 s
        # idle window would post a noisy "error" callback (Qodo PR 17
        # finding 5).
        import socket as _socket
        tel = self._telemetry
        while self._is_running:
            try:
                bytes_read = b""
                _read_start = time.monotonic()
                try:
                    bytes_read = self._stream.read(self._read_size())
                    # Some stream-like objects return None on timeout (pyserial
                    # with non-blocking reads); coerce to empty bytes.
                    if bytes_read is None:
                        bytes_read = b""
                    self._report_health(None)
                    if bytes_read:
                        tel.read_calls += 1
                        n = len(bytes_read)
                        tel.read_bytes_total += n
                        if n > tel.read_bytes_max:
                            tel.read_bytes_max = n
                except _socket.timeout:
                    # Expected idle path — no data this window. Count
                    # the timeout as idle so the gap-detection signal
                    # tracks both empty-byte returns and timeout
                    # raises (Qodo PR #68 /improve pass 3 importance 9).
                    tel.idle_reads += 1
                    if not self._is_running:
                        return
                    continue
                except Exception as exc:
                    if not self._is_running:
                        return
                    self._report_health(exc)
                    self._on_error(exc)
                    time.sleep(0.1)  # back off on error
                    continue
                finally:
                    # read_seconds covers ALL ``stream.read()`` time —
                    # successful reads, timeouts, and other exceptions.
                    # The operator needs to see time spent waiting on
                    # the device whether or not bytes arrived (Qodo PR
                    # #68 /improve pass 3 importance 9).
                    tel.read_seconds += time.monotonic() - _read_start

                # `read()` may have blocked while a stop request flipped
                # `_is_running`. Re-check before doing any further work
                # so we don't fire user callbacks for messages that
                # arrived after stop was requested (Qodo PR 17 pass 8 #3).
                if not self._is_running:
                    return

                if not bytes_read:
                    tel.idle_reads += 1
                    time.sleep(0.01)
                    continue

                with self._buffer_lock:
                    self._message_buffer.extend(bytes_read)
                    # Cap buffer growth — if the parser is making no
                    # progress (constant failure), drop the oldest
                    # half rather than running the host out of memory
                    # (Qodo PR 17 pass 17 #2). The newest bytes are
                    # the most likely to start a valid frame.
                    if len(self._message_buffer) > self._max_buffer_bytes:
                        drop = len(self._message_buffer) // 2
                        del self._message_buffer[:drop]
                    snapshot = bytes(self._message_buffer)

                # Telemetry covers BOTH successful and raising parse
                # invocations — if the parser is broken the operator
                # needs to see the time being spent (Qodo PR #68
                # review #5).
                _parse_start = time.monotonic()
                try:
                    messages, consumed = self._parser.parse_messages(snapshot)
                except Exception as exc:
                    tel.parse_seconds += time.monotonic() - _parse_start
                    tel.parse_calls += 1
                    self._on_error(exc, snapshot)
                    # Force forward progress so a parser that
                    # consistently raises on a particular byte
                    # sequence doesn't trap the consumer in an
                    # infinite re-parse loop with unbounded buffer
                    # growth (Qodo PR 17 pass 9 #2).
                    with self._buffer_lock:
                        if self._message_buffer:
                            del self._message_buffer[:1]
                    time.sleep(0.01)
                    continue
                tel.parse_seconds += time.monotonic() - _parse_start
                tel.parse_calls += 1

                # Defensive: parsers are duck-typed (the project's
                # IMessageParser is a Protocol). A faulty
                # implementation that returns negative or out-of-bounds
                # `consumed` could otherwise corrupt the message
                # buffer or trap the consumer (Qodo PR 17 pass 47 #1).
                if not isinstance(consumed, int) or consumed < 0 or consumed > len(snapshot):
                    self._on_error(
                        RuntimeError(
                            f"Parser returned invalid consumed byte count: "
                            f"{consumed!r} (buffer size: {len(snapshot)})"
                        ),
                        snapshot,
                    )
                    with self._buffer_lock:
                        if self._message_buffer:
                            del self._message_buffer[:1]
                    time.sleep(0.01)
                    continue

                # Slice the buffer down to only the bytes the parser
                # actually consumed — that's what callers expect as
                # `raw_data`. Passing the full snapshot exposes
                # trailing unparsed bytes that don't belong to any
                # of the produced messages (Qodo PR 17 pass 6 #6).
                consumed_bytes = snapshot[:consumed] if consumed > 0 else b""

                if consumed > 0:
                    with self._buffer_lock:
                        del self._message_buffer[:consumed]

                # When multiple messages came out of one parse call,
                # we can't attribute the consumed bytes to any single
                # message — sending the combined buffer to each would
                # be misleading. Pass empty bytes in that case (Qodo
                # PR 17 pass 7 #2).
                raw_for_message = consumed_bytes if len(messages) == 1 else b""

                for msg in messages:
                    # If stop() was called mid-batch (e.g. from a
                    # handler), drop the remaining messages rather
                    # than firing callbacks after shutdown begins
                    # (Qodo PR 17 pass 32 #2).
                    if not self._is_running:
                        return
                    try:
                        self._on_message(msg, raw_for_message)
                        tel.messages_emitted += 1
                    except Exception as exc:
                        self._on_error(exc, raw_for_message)
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
            # Per-handler isolation: a faulty subscriber must not
            # prevent other subscribers from receiving the same
            # message. Route exceptions to _on_error so they're
            # observable instead of silently lost (Qodo PR 17 pass
            # 10 #4). Mirrors the pattern in _on_error itself.
            try:
                h(args)
            except Exception as exc:
                self._on_error(exc, raw_data)

    def _report_health(self, error: Optional[BaseException]) -> None:
        sink = self._health_sink
        if sink is None:
            return
        try:
            if error is None:
                sink.report_io_success()
            else:
                sink.report_io_fault(error)
        except Exception:
            pass

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
