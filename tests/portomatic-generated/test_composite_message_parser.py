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
        # First message complete, second has length prefix + a partial body
        # whose first byte is a plausible protobuf field tag (0x08 = field 1,
        # wire type VARINT). The field-tag gate (PR #173) only treats
        # partial frames as garbage when the first body byte is implausible,
        # so a real partial that begins with 0x08 must be left intact for
        # the next read to complete.
        partial_body_start = bytes([0x08]) + b"nly"
        wire = (
            _length_prefix(3)
            + bytes([0x08, 0x01, 0x02])  # plausible protobuf-shaped first frame
            + _length_prefix(10)
            + partial_body_start
        )
        parser = ProtobufMessageParser(message_factory=lambda b: b)
        msgs, consumed = parser.parse_messages(wire)
        assert [m.data for m in msgs] == [bytes([0x08, 0x01, 0x02])]
        # Stops at the boundary of the incomplete second message; caller
        # can re-feed once more bytes arrive.
        assert consumed == len(_length_prefix(3)) + 3

    def test_factory_exception_skips_one_byte(self):
        def boom(_b: bytes) -> bytes:
            raise RuntimeError("bad payload")

        # The retry counter has been removed (mirrors upstream PR #169);
        # the parser now advances one byte at a time on factory failure
        # and continues until end of buffer.
        wire = _length_prefix(2) + b"xy"
        parser = ProtobufMessageParser(message_factory=boom)
        msgs, _ = parser.parse_messages(wire)
        assert msgs == []

    def test_recovers_from_leading_garbage(self):
        """Mirrors upstream PR #169 ProtobufMessageParser_ParseMessages_RecoversFromLeadingGarbage.

        Reproduces the real-world failure where boot-time garbage on the
        serial port (DTR pulse on Microchip USB-to-Serial chips, partial
        frames, etc.) leaves unrecognizable bytes at the head of the
        consumer buffer. When a valid frame arrives behind the garbage,
        the parser must skip past it and parse the valid frame.

        With the retry-counter cap, the parser exited after a few failed
        attempts and never advanced past the garbage; subsequent reads
        kept stacking bytes onto a buffer the parser refused to drain.
        """
        # 20 bytes of 0x00 — each is a valid but zero-length varint
        # prefix (length=0). Under the new "messageLength <= 0 advances
        # one byte" rule, the parser walks past all 20 bytes, then
        # parses the valid frame behind them.
        junk = b"\x00" * 20
        payload = b"valid-frame-payload"
        valid_frame = _length_prefix(len(payload)) + payload

        wire = junk + valid_frame
        parser = ProtobufMessageParser(message_factory=lambda b: ("parsed", b))
        msgs, consumed = parser.parse_messages(wire)

        assert len(msgs) == 1
        assert msgs[0].data == ("parsed", payload)
        assert consumed == len(wire)

    def test_recovers_from_oversized_prefix(self):
        """Mirrors upstream PR #173 ProtobufMessageParser_ParseMessages_RecoversFromOversizedPrefix.

        Reproduces the streaming-startup stall where a plausible-looking
        but bogus varint prefix declares a message size in the tens of
        KB. Under the old 1 MB cap, the parser sat waiting for that much
        data forever. The 4 KB cap rejects such a prefix immediately and
        resyncs one byte at a time.
        """
        # Varint encoding 32768 = (0 << 0) | (0 << 7) | (2 << 14):
        # the first two bytes set the continuation bit, the third
        # terminates. Plausible under the old 1 MB cap but well over
        # the new 4 KB cap.
        bogus_prefix = bytes([0x80, 0x80, 0x02])

        # Real frames start with a protobuf field tag. Mirror that here
        # so the resync path doesn't pick up spurious matches against
        # the bogus prefix bytes — 0x08 is field 1, wire type VARINT.
        # SENTINEL_PAYLOAD is what a strict factory will accept.
        sentinel_payload = bytes([0x08]) + b"valid-frame-marker"
        valid_frame = _length_prefix(len(sentinel_payload)) + sentinel_payload

        # Strict factory rejects anything that isn't our real frame.
        # Mimics the protobuf parser, which rejects malformed bytes —
        # without this, the lambda accepts every spurious resync slice.
        def strict_factory(b: bytes):
            if b != sentinel_payload:
                raise ValueError("not the real frame")
            return ("parsed", b)

        wire = bogus_prefix + valid_frame
        parser = ProtobufMessageParser(message_factory=strict_factory)
        msgs, consumed = parser.parse_messages(wire)

        assert len(msgs) == 1
        assert msgs[0].data == ("parsed", sentinel_payload)
        assert consumed == len(wire)

    def test_large_multi_chunk_frame_not_misclassified(self):
        """Regression: a legitimate multi-KB frame arriving in chunks
        must not be eaten by the gap gate.

        From Qodo /agentic_review on PR #100: the original gap-gate
        check fired whenever ``missing_payload > 1024``, which would
        mis-classify any real frame > ~1 KB until it was nearly
        complete — corrupting the frame because callers drop
        ``consumed`` bytes unconditionally. Fix: only apply the gap
        heuristic when ≤1 body byte has arrived.
        """
        # Build a 2 KB legitimate payload that begins with a valid
        # protobuf field tag (0x08 = field 1, wire VARINT). The varint
        # length-prefix for 2048 is two bytes ([0x80, 0x10]).
        sentinel_payload = bytes([0x08]) + b"X" * 2047
        full_frame = _length_prefix(len(sentinel_payload)) + sentinel_payload

        # First chunk: just the prefix + 50 body bytes. missing_payload
        # = 1998 (>> 1024). Without the fix, gap gate would fire and
        # advance into the real payload.
        first_chunk = full_frame[: 2 + 50]

        parser = ProtobufMessageParser(message_factory=lambda b: ("parsed", b))

        # First call: not enough data. Parser must wait, NOT advance.
        msgs1, consumed1 = parser.parse_messages(first_chunk)
        assert msgs1 == []
        assert consumed1 == 0  # nothing consumed — caller retains full chunk

        # Second call: full frame arrives. Should now parse cleanly.
        msgs2, consumed2 = parser.parse_messages(full_frame)
        assert len(msgs2) == 1
        assert msgs2[0].data == ("parsed", sentinel_payload)
        assert consumed2 == len(full_frame)

    def test_one_body_byte_with_huge_gap_does_not_fire_gap_gate(self):
        """Regression: a real >1KB partial frame whose first chunk
        contains the prefix + exactly one body byte (a valid field
        tag) must not be eaten by the gap gate.

        From Qodo /improve pass 3 on PR #100: my pass 1 fix used
        `available_body_bytes <= 1` which still fired the gap gate
        when 1 body byte was available, even though the field-tag
        gate could have decided on its own. Tightened to `== 0`.
        """
        # 2 KB payload, valid field tag at byte 0
        sentinel_payload = bytes([0x08]) + b"Y" * 2047
        full_frame = _length_prefix(len(sentinel_payload)) + sentinel_payload
        # Slice off ALL but prefix + 1 byte to hit the edge case:
        # prefix=2 bytes, body=1 byte, missing_payload = 2047 (> 1024)
        first_chunk = full_frame[: 2 + 1]

        parser = ProtobufMessageParser(message_factory=lambda b: ("parsed", b))

        # Pass 1's `<= 1` gate would have fired here (gap > 1024,
        # body=1) and corrupted the frame. Pass 3's `== 0` lets the
        # field-tag check (byte 0x08 = valid VARINT tag) handle it,
        # so parser correctly waits.
        msgs1, consumed1 = parser.parse_messages(first_chunk)
        assert msgs1 == []
        assert consumed1 == 0  # nothing consumed — caller retains chunk

        # Full frame arrives later — parses cleanly.
        msgs2, consumed2 = parser.parse_messages(full_frame)
        assert len(msgs2) == 1
        assert msgs2[0].data == ("parsed", sentinel_payload)

    def test_field_tag_byte_rejects_invalid_wire_type_with_continuation(self):
        """Regression: a byte with the continuation bit set must still
        have its wire-type bits validated.

        From Qodo /agentic_review on PR #100: previously,
        ``_is_plausible_field_tag_byte`` early-returned True for any
        byte with bit 0x80 set, bypassing wire-type validation. Wire
        type lives in the low 3 bits regardless of whether the field
        number spans multiple bytes — types 3/4/6/7 should be rejected
        even on continuation bytes.
        """
        from daqifi.communication import _is_plausible_field_tag_byte

        # Continuation bit set, wire type 0 (VARINT) — should pass
        # (multi-byte field number, but valid wire type).
        assert _is_plausible_field_tag_byte(0x80) is True
        # Continuation bit set, wire type 2 (LEN) — should pass.
        assert _is_plausible_field_tag_byte(0x82) is True
        # Continuation bit set, wire type 3 (deprecated SGROUP) — must reject.
        assert _is_plausible_field_tag_byte(0x83) is False
        # Continuation bit set, wire type 4 (deprecated EGROUP) — must reject.
        assert _is_plausible_field_tag_byte(0x84) is False
        # Continuation bit set, wire type 6 (undefined) — must reject.
        assert _is_plausible_field_tag_byte(0x86) is False
        # Continuation bit set, wire type 7 (undefined) — must reject.
        assert _is_plausible_field_tag_byte(0x87) is False

    def test_preserves_partial_frame_after_garbage(self):
        """Mirrors upstream PR #173 ProtobufMessageParser_ParseMessages_PreservesPartialFrameAfterGarbage.

        Guards the consumer contract: when leading garbage is followed
        by the beginning of a real frame whose payload hasn't fully
        arrived yet, the parser must consume only the garbage. The
        consumer trims ``consumed`` bytes from its buffer
        unconditionally, so advancing even one byte into the real prefix
        or payload here would permanently corrupt the frame on the next
        read.
        """
        # 20 zero bytes — each decodes as a zero-length varint prefix
        # and is skipped one byte at a time.
        junk = b"\x00" * 20

        # Build a full valid frame and keep only the prefix + first body
        # byte to simulate an incomplete arrival. The first body byte
        # must be a plausible field tag (so the field-tag gate doesn't
        # reject it as garbage). Field 1, wire type 0 (VARINT) → tag
        # byte 0x08.
        payload_full = bytes([0x08]) + b"\x2a" + b"more-payload-tail"
        valid_frame_prefix = _length_prefix(len(payload_full))
        partial = valid_frame_prefix + bytes([0x08])

        wire = junk + partial
        parser = ProtobufMessageParser(message_factory=lambda b: b)
        msgs, consumed = parser.parse_messages(wire)

        # No frame parsed yet, and consumed stops exactly at the real
        # frame boundary (junk only). Caller retains the partial.
        assert msgs == []
        assert consumed == len(junk)


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
