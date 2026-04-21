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
from typing import Generic, List, Tuple, TypeVar


__all__ = ["InboundMessage", "TextInboundMessage", "LineBasedMessageParser"]


T = TypeVar("T")


@dataclass(frozen=True)
class InboundMessage(Generic[T]):
    """Parsed inbound message — the Python equivalent of `IInboundMessage<T>`."""

    data: T


# Concrete alias matching the C# `TextInboundMessage` name. Same shape
# as `InboundMessage[str]` — kept as its own type for documentation
# clarity and parity with C# call sites.
TextInboundMessage = InboundMessage[str]


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
