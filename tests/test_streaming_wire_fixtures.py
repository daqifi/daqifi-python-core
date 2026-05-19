"""Regression fixtures captured from real DAQiFi hardware.

Each fixture in this file is a verbatim wire-bytes snapshot produced
by firmware during a documented streaming scenario. The point is to
lock in parser/dispatch correctness against the actual byte shapes
the firmware emits — synthesized protobuf payloads can drift from
what real hardware produces (different field-presence rules, varint
encoding choices, side-channel fields like ``digital_port_dir`` that
some firmware revisions include in stream frames).

When adding a new fixture: include the firmware version, capture
date, SCPI configuration, and the hex dump in a comment block, plus
a link to the GitHub issue or commit where the bytes were captured.
"""

from __future__ import annotations

import pytest

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.communication import (
    CompositeMessageParser,
    ProtobufMessageParser,
)
from daqifi.enums import ProtobufMessageType
from daqifi.protocol import ProtobufProtocolHandler


# -- Fixture 1: single-channel @ 100 Hz, fw 3.4.6b1 ---------------------------
#
# Captured 2026-04-23 by the firmware-side agent on
# daqifi-nyquist-firmware main, in response to GitHub issue
# daqifi/daqifi-nyquist-firmware#358. SCPI configuration:
#
#   ENAble:VOLTage:DC 1                    (bitmask form, ch0 only)
#   SYSTem:STReam:FORmat 0                 (protobuf)
#   SYSTem:StartStreamData 100             (100 Hz)
#
# Wire bytes (one frame, 19 bytes total):
#
#   12 08 c6 ec d8 e3 04   12 01 00   2a 02 00 00   aa 02 02 ff ff
#   ^^                     ^^         ^^            ^^
#   |                      |          |             field 37 tag
#   |                      |          field 5 tag (digital_data)
#   |                      field 2 tag (analog_in_data, packed)
#   outer varint length prefix (=18, the payload size)
#
# The same frame is emitted 200× over a 2-second capture. Per the
# firmware agent's source-level inspection of SCPIInterface.c:3436
# and SCPIADC.c:128, both `ENAble:VOLTage:DC <mask>` and
# `CONFigure:ADC:CHANnel <ch> <state>` route to the same callback
# (`SCPI_ADCChanEnableSet`) and produce byte-for-byte identical
# stream output — only the SCPI dispatch path differs.

_FW_3_4_6B1_1CH_100HZ_FRAME = bytes([
    0x12, 0x08, 0xc6, 0xec, 0xd8, 0xe3, 0x04, 0x12, 0x01, 0x00,
    0x2a, 0x02, 0x00, 0x00, 0xaa, 0x02, 0x02, 0xff, 0xff,
])


class TestSingleChannel100HzFw346b1:
    """Lock in correct parser behavior for the captured 1ch frame.

    Background: a hardware test from the python-core PR #17 cycle
    initially reported "0 frames received" when streaming a single
    channel at 100 Hz — it looked like a parser regression. Tracing
    the captured bytes (this fixture) through the parser stack shows
    the parser produces the correct frame count and dispatches each
    frame to the STREAM handler. The 0-frames symptom must therefore
    have come from somewhere else in the test setup (driver layer,
    USB-CDC quirk, init-sequence race), NOT from the protobuf path.
    These tests guard against any future regression that would
    actually break the parser.
    """

    FRAME_LEN = 19  # 1-byte varint prefix + 18-byte payload

    def test_frame_size_invariant(self):
        # Mostly a sanity assert — if this test starts failing, someone
        # edited the fixture bytes by mistake and the rest of the file
        # will be testing a different shape than the comment claims.
        assert len(_FW_3_4_6B1_1CH_100HZ_FRAME) == self.FRAME_LEN
        assert _FW_3_4_6B1_1CH_100HZ_FRAME[0] == 18

    def test_payload_decodes_to_expected_protobuf(self):
        msg = DaqifiOutMessage.FromString(_FW_3_4_6B1_1CH_100HZ_FRAME[1:])
        assert msg.msg_time_stamp == 1282815558
        assert list(msg.analog_in_data) == [0]
        assert list(msg.analog_in_data_float) == []
        assert msg.digital_data == b"\x00\x00"
        assert msg.digital_port_dir == b"\xff\xff"
        # Critical: device_status must NOT be set, otherwise the
        # protocol handler would route the frame to ERROR.
        assert msg.device_status == 0

    def test_protobuf_parser_consumes_full_stream(self):
        # 200 frames was the firmware agent's exact capture window
        # (100 Hz × 2 s). Verifying full consumption rules out the
        # "trailing-byte stuck in buffer" failure mode.
        stream = _FW_3_4_6B1_1CH_100HZ_FRAME * 200
        parser = ProtobufMessageParser(DaqifiOutMessage.FromString)
        messages, consumed = parser.parse_messages(stream)
        assert len(messages) == 200
        assert consumed == len(stream)

    def test_composite_parser_routes_to_protobuf(self):
        # Composite is what the StreamMessageConsumer actually uses —
        # if the heuristic mis-classifies a 1ch stream as text, the
        # frames go to LineBasedMessageParser and silently get lost.
        stream = _FW_3_4_6B1_1CH_100HZ_FRAME * 50
        parser = CompositeMessageParser(
            protobuf_factory=DaqifiOutMessage.FromString
        )
        messages, consumed = parser.parse_messages(stream)
        assert len(messages) == 50
        assert consumed == len(stream)

    def test_dispatch_routes_each_frame_to_stream_handler(self):
        # The ProtobufProtocolHandler must classify this as STREAM
        # — not STATUS (no port-count fields), not ERROR (device_status
        # is zero), not UNKNOWN (msg_time_stamp + analog_in_data are
        # both populated).
        stream = _FW_3_4_6B1_1CH_100HZ_FRAME * 200
        parser = CompositeMessageParser(
            protobuf_factory=DaqifiOutMessage.FromString
        )
        messages, _ = parser.parse_messages(stream)

        counts = {"stream": 0, "status": 0, "error": 0, "samples": 0}

        def on_stream(msg):
            counts["stream"] += 1
            counts["samples"] += len(msg.analog_in_data)

        def on_status(msg):
            counts["status"] += 1

        def on_error(msg):
            counts["error"] += 1

        handler = ProtobufProtocolHandler(
            stream_message_handler=on_stream,
            status_message_handler=on_status,
            error_message_handler=on_error,
        )
        for m in messages:
            handler.handle(m)

        assert counts["stream"] == 200
        assert counts["status"] == 0
        assert counts["error"] == 0
        # Single-channel frame carries exactly one packed sint32.
        assert counts["samples"] == 200

    def test_detect_message_type_is_stream(self):
        # Direct unit assertion (no parser/dispatch in the loop) that
        # the captured frame's protobuf shape classifies as STREAM.
        msg = DaqifiOutMessage.FromString(_FW_3_4_6B1_1CH_100HZ_FRAME[1:])
        assert (
            ProtobufProtocolHandler.detect_message_type(msg)
            is ProtobufMessageType.STREAM
        )

    @pytest.mark.parametrize(
        "split_at",
        [1, 5, 10, 18, 19, 20, 25, 38],
    )
    def test_partial_frame_does_not_misclassify(self, split_at):
        # The serial transport delivers bytes in arbitrary chunks.
        # If the parser classifies a partial frame as text or
        # mis-advances the buffer, the consumer drops bytes and the
        # frame count goes south. Each split point here represents a
        # plausible OS-level read boundary (mid-prefix, mid-payload,
        # one full frame + partial next).
        stream = _FW_3_4_6B1_1CH_100HZ_FRAME * 2
        head, tail = stream[:split_at], stream[split_at:]
        parser = CompositeMessageParser(
            protobuf_factory=DaqifiOutMessage.FromString
        )
        m1, c1 = parser.parse_messages(head)
        # After consuming what we can from the head, the rest must be
        # parseable when tail bytes arrive (head[c1:] + tail).
        leftover = head[c1:] + tail
        m2, c2 = parser.parse_messages(leftover)
        assert len(m1) + len(m2) == 2
        assert c1 + c2 == len(stream)
