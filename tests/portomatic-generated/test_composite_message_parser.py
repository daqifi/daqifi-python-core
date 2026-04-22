"""Tests for the CompositeMessageParser + ProtobufMessageParser +
ObjectInboundMessage ports (daqifi/communication.py). Upstream snapshot e8d84af."""

import pytest

from daqifi.communication import (
    CompositeMessageParser,
    LineBasedMessageParser,
    ObjectInboundMessage,
    ProtobufMessageParser,
)


pytestmark = pytest.mark.portomatic_generated


class TestObjectInboundMessage:
    def test_holds_arbitrary_data(self):
        msg = ObjectInboundMessage(data={"foo": 1})
        assert msg.data == {"foo": 1}

    def test_is_frozen(self):
        msg = ObjectInboundMessage(data="x")
        with pytest.raises(Exception):
            msg.data = "y"  # type: ignore[misc]


def _length_prefix(n: int) -> bytes:
    """Encode ``n`` as a protobuf-style varint (uint32)."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class TestProtobufMessageParser:
    def test_empty_input_returns_nothing(self):
        parser = ProtobufMessageParser(message_factory=lambda b: b)
        msgs, consumed = parser.parse_messages(b"")
        assert msgs == []
        assert consumed == 0

    def test_parses_one_length_prefixed_payload(self):
        payload = b"hello"
        wire = _length_prefix(len(payload)) + payload
        parser = ProtobufMessageParser(message_factory=lambda b: ("parsed", b))
        msgs, consumed = parser.parse_messages(wire)
        assert consumed == len(wire)
        assert len(msgs) == 1
        assert msgs[0].data == ("parsed", payload)

    def test_parses_multiple_payloads(self):
        wire = _length_prefix(3) + b"abc" + _length_prefix(2) + b"de"
        parser = ProtobufMessageParser(message_factory=lambda b: b)
        msgs, consumed = parser.parse_messages(wire)
        assert consumed == len(wire)
        assert [m.data for m in msgs] == [b"abc", b"de"]

    def test_partial_tail_leaves_consumed_short(self):
        # First message complete, second has length prefix but missing payload.
        wire = _length_prefix(3) + b"abc" + _length_prefix(10) + b"only"
        parser = ProtobufMessageParser(message_factory=lambda b: b)
        msgs, consumed = parser.parse_messages(wire)
        assert [m.data for m in msgs] == [b"abc"]
        # Stops before the incomplete second message; caller can re-feed.
        assert consumed == len(_length_prefix(3)) + 3

    def test_factory_exception_skips_one_byte(self):
        def boom(_b: bytes) -> bytes:
            raise RuntimeError("bad payload")

        # 3 retries before bailing -> consumes 3 bytes max with this input.
        wire = _length_prefix(2) + b"xy"
        parser = ProtobufMessageParser(message_factory=boom)
        msgs, _ = parser.parse_messages(wire)
        assert msgs == []


class TestCompositeMessageParser:
    def test_routes_text_to_text_parser(self):
        parser = CompositeMessageParser()
        msgs, consumed = parser.parse_messages(b"*IDN?\r\nREAD?\r\n")
        assert [m.data for m in msgs] == ["*IDN?", "READ?"]
        assert consumed == len(b"*IDN?\r\nREAD?\r\n")

    def test_returns_object_inbound_messages(self):
        parser = CompositeMessageParser()
        msgs, _ = parser.parse_messages(b"*IDN?\r\n")
        assert all(isinstance(m, ObjectInboundMessage) for m in msgs)

    def test_routes_binary_to_protobuf_parser(self):
        # 30% null bytes -> hint LIKELY_PROTOBUF.
        binary = b"\x05" + b"\x00\x00\x00\x00\x00"  # length 5 + 5 nulls
        captured = []

        def factory(b: bytes) -> str:
            captured.append(b)
            return "P"

        parser = CompositeMessageParser(protobuf_factory=factory)
        msgs, consumed = parser.parse_messages(binary)
        assert [m.data for m in msgs] == ["P"]
        assert consumed == len(binary)
        assert captured == [b"\x00\x00\x00\x00\x00"]

    def test_empty_input(self):
        parser = CompositeMessageParser()
        assert parser.parse_messages(b"") == ([], 0)

    def test_falls_back_to_other_parser_when_first_fails(self):
        # Looks textish (has trailing \r\n) but text parser yields nothing
        # if there's no actual line content. Should then try protobuf,
        # which will also yield nothing — final result is empty.
        parser = CompositeMessageParser()
        msgs, _ = parser.parse_messages(b"\r\n")
        assert msgs == []

    def test_custom_text_parser_is_respected(self):
        custom_text = LineBasedMessageParser(line_ending="\n")
        parser = CompositeMessageParser(text_parser=custom_text)
        msgs, _ = parser.parse_messages(b"*IDN?\nREAD?\n")
        assert [m.data for m in msgs] == ["*IDN?", "READ?"]
