"""Tests for `daqifi.communication` (port of LineBasedMessageParser)."""
from __future__ import annotations

import pytest

from daqifi.communication import (
    InboundMessage,
    LineBasedMessageParser,
    TextInboundMessage,
)


class TestInboundMessage:
    def test_holds_data(self):
        m = InboundMessage(data="hello")
        assert m.data == "hello"

    def test_text_inbound_message_alias(self):
        m: TextInboundMessage = InboundMessage(data="hi")
        assert isinstance(m, InboundMessage)
        assert m.data == "hi"


class TestLineBasedMessageParser:
    def test_empty_data(self):
        parser = LineBasedMessageParser()
        messages, consumed = parser.parse_messages(b"")
        assert messages == []
        assert consumed == 0

    def test_single_complete_line(self):
        parser = LineBasedMessageParser()
        messages, consumed = parser.parse_messages(b"hello\r\n")
        assert len(messages) == 1
        assert messages[0].data == "hello"
        assert consumed == len(b"hello\r\n")

    def test_multiple_complete_lines(self):
        parser = LineBasedMessageParser()
        messages, consumed = parser.parse_messages(b"first\r\nsecond\r\nthird\r\n")
        assert [m.data for m in messages] == ["first", "second", "third"]
        assert consumed == len(b"first\r\nsecond\r\nthird\r\n")

    def test_partial_trailing_line_not_consumed(self):
        parser = LineBasedMessageParser()
        messages, consumed = parser.parse_messages(b"first\r\nsecond_partial")
        assert [m.data for m in messages] == ["first"]
        # Trailing partial bytes are NOT consumed — caller can keep accumulating.
        assert consumed == len(b"first\r\n")

    def test_blank_lines_skipped(self):
        parser = LineBasedMessageParser()
        messages, consumed = parser.parse_messages(b"first\r\n\r\nsecond\r\n")
        assert [m.data for m in messages] == ["first", "second"]
        assert consumed == len(b"first\r\n\r\nsecond\r\n")

    def test_whitespace_only_line_preserved(self):
        # Pass 46 #1: only line endings are stripped, not interior or
        # leading/trailing whitespace — SCPI responses can contain
        # whitespace-padded fields that callers depend on.
        parser = LineBasedMessageParser()
        messages, _ = parser.parse_messages(b"first\r\n   \r\nsecond\r\n")
        assert [m.data for m in messages] == ["first", "   ", "second"]

    def test_message_data_preserves_inner_whitespace(self):
        # Pass 46 #1: only \r\n is stripped; surrounding whitespace
        # stays so a SCPI value like "  X1234  " round-trips intact.
        parser = LineBasedMessageParser()
        messages, _ = parser.parse_messages(b"  spaced  \r\n")
        assert messages[0].data == "  spaced  "

    def test_no_line_ending_returns_no_messages(self):
        parser = LineBasedMessageParser()
        messages, consumed = parser.parse_messages(b"no terminator here")
        assert messages == []
        assert consumed == 0

    def test_custom_line_ending_lf_only(self):
        parser = LineBasedMessageParser(line_ending="\n")
        messages, consumed = parser.parse_messages(b"a\nb\nc\n")
        assert [m.data for m in messages] == ["a", "b", "c"]
        assert consumed == len(b"a\nb\nc\n")

    def test_custom_encoding_utf8_multibyte(self):
        parser = LineBasedMessageParser()
        # "café" is two-byte 'é' in UTF-8.
        messages, _ = parser.parse_messages("café\r\n".encode("utf-8"))
        assert messages[0].data == "café"

    def test_carriage_return_only_line_ending(self):
        parser = LineBasedMessageParser(line_ending="\r")
        messages, _ = parser.parse_messages(b"x\ry\r")
        assert [m.data for m in messages] == ["x", "y"]

    def test_empty_line_ending_rejected(self):
        # Qodo PR #11 pass-4 finding S1: an empty line_ending would put
        # `parse_messages` into an infinite loop because `bytes.find("")`
        # always returns 0.
        with pytest.raises(ValueError):
            LineBasedMessageParser(line_ending="")

    def test_malformed_bytes_do_not_crash(self):
        # `errors="replace"` keeps the parser resilient against partial
        # multi-byte sequences in the middle of a serial stream.
        parser = LineBasedMessageParser()
        bad = b"\xff\xfe\xfdabc\r\n"
        messages, consumed = parser.parse_messages(bad)
        assert len(messages) == 1
        assert "abc" in messages[0].data
        assert consumed == len(bad)


# ---- ProtobufMessageParser: text-prefix resilience -----------------------


class TestProtobufMessageParserTextPrefixResilience:
    """Regression for the 2026-04-23 hardware finding: USB serial puts
    SCPI text echoes (``DAQIFI> ``, ``**ERROR ...``) into the buffer
    ahead of valid protobuf streaming frames. The parser must scan past
    them rather than bailing after a small retry budget — otherwise
    the consumer's buffer grows unbounded and no streaming messages
    ever surface."""

    def _make_parser(self, factory=None):
        from daqifi.communication import ProtobufMessageParser
        if factory is None:
            factory = lambda b: b  # accept anything for byte-level checks
        return ProtobufMessageParser(message_factory=factory)

    def test_scans_past_failures_without_retry_limit(self):
        # Regression for the 2026-04-23 finding: the original parser had
        # `retry_count < _MAX_RETRY_ATTEMPTS` (= 3) which meant after 3
        # consecutive parse failures the loop would bail with a tiny
        # `consumed` value, leaving the rest of the buffer untouched.
        # On real hardware the buffer often has long SCPI text echoes
        # (`DAQIFI> ...`) ahead of valid protobuf frames; with the gate
        # the parser never reached the valid frame.
        #
        # Rejector factory raises for every payload — simulates the
        # text-prefix case where every prefix-byte advance fails.
        # The parser MUST keep scanning to the end of `data` rather
        # than stopping early. Concretely: `consumed` should equal
        # `len(data)` after a full scan.
        def reject(_):
            raise ValueError("not a protobuf frame")
        parser = self._make_parser(factory=reject)
        # Each byte is a small length-prefix that triggers a slice +
        # parse attempt; rejector causes failure each time.
        data = bytes([5, 1, 2, 3, 4, 5,    # 1 frame attempt
                      5, 1, 2, 3, 4, 5,    # 2 attempts
                      5, 1, 2, 3, 4, 5,    # 3 attempts (was bail point)
                      5, 1, 2, 3, 4, 5,    # 4 attempts
                      5, 1, 2, 3, 4, 5])   # 5 attempts — must reach end
        _, consumed = parser.parse_messages(data)
        # Old retry-limit bug capped consumed at ~3 bytes. Post-fix,
        # parser scans the full buffer (with normal "wait for more
        # data" behaviour at the trailing edge if a frame can't fit).
        # Either way, consumed should be much larger than the old gate.
        assert consumed > 10, (
            f"parser stopped early at {consumed}/{len(data)} after "
            f"only a few failures — the retry-limit gate is back"
        )

    def test_parses_many_back_to_back_frames(self):
        # 50 valid frames, no text noise. Validates the basic
        # length-prefix + payload loop runs to completion.
        parser = self._make_parser()  # permissive factory
        frame = bytes([3, 0xaa, 0xbb, 0xcc])
        data = frame * 50
        messages, consumed = parser.parse_messages(data)
        assert len(messages) == 50
        assert consumed == len(data)
