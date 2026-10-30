"""#204: protocol parsing.

1. Echo text ahead of a short protobuf frame stalled ProtobufMessageParser:
   the echo's 'S' (0x53) decodes as an 83-byte length with a plausible 'Y'
   after it, so the parser waited for bytes that never came while the real
   frame sat behind it (upstream ed605b8).
2. CompositeMessageParser parsed a binary frame ending in CRLF (or 0x0A) as
   text: a trailing line ending counted as a text marker, and it was checked
   before null density; the line parser then swallowed the frame (upstream
   e8c4998 / 41dbf3d).
3. TimestampProcessor used a fixed 20 ns tick, not the device's
   timestamp_freq (upstream 6f9677d).
"""

from datetime import timedelta

import pytest

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.communication import CompositeMessageParser, ProtobufMessageParser
from daqifi.timestamp import TimestampProcessor


def _frame(message) -> bytes:
    body = message.SerializeToString()
    out = bytearray()
    n = len(body)
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + body


def _status():
    # A status reply: 16 zero calibration offsets make it null-heavy, as the
    # real SYSInfoPB? reply is (~40% null bytes).
    return DaqifiOutMessage(
        analog_in_port_num=16, device_sn=1234, analog_in_cal_b=[0.0] * 16
    )


SHORT = DaqifiOutMessage(msg_time_stamp=4, analog_in_data=[5])  # 05 08 04 12 01 0a (zigzag 5)


# ---- 1: echo ahead of a short frame -------------------------------------


@pytest.mark.parametrize("noise", [b"SYSTem:SYSInfoPB?\r\n", b"DAQIFI> ", b"SYSTem:SYSInfoPB?\r\nDAQIFI> "])
def test_text_ahead_of_a_short_frame_is_skipped(noise) -> None:
    data = noise + _frame(SHORT)
    parser = ProtobufMessageParser(DaqifiOutMessage.FromString)
    messages, consumed = parser.parse_messages(data)
    assert [m.data for m in messages] == [SHORT]
    assert consumed == len(data)


def test_a_real_frame_arriving_in_chunks_still_waits() -> None:
    # Its body holds nested fields that would parse as frames on their own;
    # the look-ahead mustn't skip into them.
    frame = _frame(DaqifiOutMessage(
        analog_in_port_num=16, device_sn=1234, device_pn="Nq1",
        analog_in_cal_m=[1.0] * 16, analog_in_cal_b=[0.5] * 16,
    ))
    parser = ProtobufMessageParser(DaqifiOutMessage.FromString)
    for cut in range(3, len(frame), 7):
        messages, consumed = parser.parse_messages(frame[:cut])
        assert messages == [] and consumed == 0, cut
    messages, consumed = parser.parse_messages(frame)
    assert len(messages) == 1 and consumed == len(frame)


# ---- 2: binary frames ending in a line ending ---------------------------


def _composite():
    return CompositeMessageParser(protobuf_factory=DaqifiOutMessage.FromString)


def test_a_status_frame_ending_in_crlf_is_parsed_as_protobuf() -> None:
    data = _frame(_status()) + b"\r\n"
    messages, consumed = _composite().parse_messages(data)
    assert messages and isinstance(messages[0].data, DaqifiOutMessage)
    assert messages[0].data.analog_in_port_num == 16


def test_an_echoed_status_reply_is_parsed_as_protobuf() -> None:
    # Echo on: the echoed command, the binary reply, then CRLF.
    data = b"SYSTem:SYSInfoPB?\r\n" + _frame(_status()) + b"\r\n"
    messages, consumed = _composite().parse_messages(data)
    assert messages and isinstance(messages[0].data, DaqifiOutMessage)


@pytest.mark.parametrize(
    "message, ending",
    [
        (SHORT, b"\n"),
        # 06 08 04 12 02 0d 0a: zigzag -7 and 5, a frame ending in CRLF.
        (DaqifiOutMessage(msg_time_stamp=4, analog_in_data=[-7, 5]), b"\r\n"),
    ],
    ids=["lf", "crlf"],
)
def test_a_short_streaming_frame_ending_in_a_line_ending_is_parsed_as_protobuf(message, ending) -> None:
    data = _frame(message)
    assert data.endswith(ending) and 0 not in data
    messages, consumed = _composite().parse_messages(data)
    assert [m.data for m in messages] == [message]
    assert consumed == len(data)


def test_scpi_text_is_still_parsed_as_text() -> None:
    messages, consumed = _composite().parse_messages(b'0,"No error"\r\n')
    assert [m.data for m in messages] == ['0,"No error"']


# ---- 3: per-device tick frequency ---------------------------------------


def _gap_seconds(processor, device, cycles):
    processor.process_timestamp(device, 1000)
    return processor.process_timestamp(device, 1000 + cycles).seconds_between_messages


def test_a_device_frequency_sets_its_tick_period() -> None:
    processor = TimestampProcessor()
    processor.set_timestamp_frequency("nq1", 80_000_000)
    assert processor.get_tick_period("nq1") == pytest.approx(1 / 80_000_000)
    assert _gap_seconds(processor, "nq1", 80_000_000) == pytest.approx(1.0)
    # Another device keeps the 20 ns fallback.
    assert _gap_seconds(processor, "other", 50_000_000) == pytest.approx(1.0)


def test_zero_clears_a_device_frequency_and_reset_keeps_it() -> None:
    processor = TimestampProcessor()
    processor.set_timestamp_frequency("nq1", 80_000_000)
    processor.reset("nq1")
    assert processor.get_tick_period("nq1") == pytest.approx(1 / 80_000_000)
    processor.set_timestamp_frequency("nq1", 0)
    assert processor.get_tick_period("nq1") == TimestampProcessor.DEFAULT_TICK_PERIOD
    processor.set_timestamp_frequency("nq1", 80_000_000)
    processor.reset_all()
    assert processor.get_tick_period("nq1") == TimestampProcessor.DEFAULT_TICK_PERIOD


@pytest.mark.parametrize("bad, error", [(-1, ValueError), (1.5, TypeError), (True, TypeError)])
def test_an_invalid_frequency_is_refused(bad, error) -> None:
    with pytest.raises(error):
        TimestampProcessor().set_timestamp_frequency("nq1", bad)
