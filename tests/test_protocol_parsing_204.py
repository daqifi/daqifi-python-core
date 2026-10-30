"""#204.

3. TimestampProcessor used a fixed 20 ns tick, not the device's
   timestamp_freq (upstream 6f9677d). Fixed here.

Items 1 (echo text ahead of a short frame stalls ProtobufMessageParser) and
2 (CompositeMessageParser reads a binary frame ending in CRLF as text) are
deferred to #231: two audit rounds showed that without knowing whether the
device is in text or streaming mode, no byte-level rule tells text from the
start of a binary frame. Every attempt either ate partial frames or starved
complete text. The eight audit repros below are the acceptance set for that
ticket. The parser on main handles all of them, and they pin that it still
does.
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


def _composite():
    return CompositeMessageParser(protobuf_factory=DaqifiOutMessage.FromString)


def _protobuf():
    return ProtobufMessageParser(DaqifiOutMessage.FromString)


# ---- the acceptance set for #231: never discard a partial frame, never
# ---- starve complete text (audit rounds 1 and 2, exact bytes) -------------


@pytest.mark.parametrize(
    "data",
    [
        # First 5 bytes of 0a 2a 02 08 01 40 01 48 01 50 01: a printable
        # length (0x0a) and tag (0x2a).
        bytes.fromhex("0a2a020801"),
        # First 10 bytes of a 0x24-length frame.
        _frame(DaqifiOutMessage(
            msg_time_stamp=40, analog_in_data=[1, 4] * 3 + [8192] * 8 + [1, 4]
        ))[:10],
        # A partial frame whose digital payload starts "A*?abc\r\n" and then
        # holds bytes that parse as a frame by themselves.
        (bytes.fromhex("412a3f6162630d0a05080412010a") + b"x" * 52)[:14],
        # The same with "A2?AAAA\r\n" (digital_data_ts).
        _frame(DaqifiOutMessage(
            digital_data_ts=[65] * 4 + [13, 10] + [5, 8, 4, 18, 1, 10] + [65] * 51
        ))[:15],
    ],
    ids=["printable-length-and-tag", "0x24-frame", "A*?abc-payload", "A2?AAAA-payload"],
)
def test_a_partial_frame_is_never_skipped_into(data) -> None:
    assert _protobuf().parse_messages(data) == ([], 0)


def test_a_partial_frame_completes_once_the_rest_arrives() -> None:
    frame = bytes.fromhex("412a3f6162630d0a05080412010a") + b"x" * 52
    parser = _protobuf()
    assert parser.parse_messages(frame[:14]) == ([], 0)
    messages, consumed = parser.parse_messages(frame)
    assert len(messages) == 1 and consumed == len(frame)


def test_a_float_payload_with_printable_bytes_and_crlf_is_one_protobuf_frame() -> None:
    # A complete 69-byte frame: timestamp 64, eight 2.5006... and eight
    # 3.0195... floats, whose bytes are "\r\n @" and "\x00AA@".
    data = bytes.fromhex("4408401a40") + b"\r\n @" * 8 + b"\x00AA@" * 8
    messages, consumed = _composite().parse_messages(data)
    assert len(messages) == 1 and isinstance(messages[0].data, DaqifiOutMessage)
    assert messages[0].data.msg_time_stamp == 64
    assert consumed == 69


@pytest.mark.parametrize(
    "data, waiting_after",
    [
        # A CRLF separator, then the first 20 of a 69-byte frame's bytes.
        (b"\r\n" + bytes.fromhex("44082a1a40") + b"\r\n @" + b"\x00" * 11, 2),
        # One noise byte, then 0a0801120600000d0ac801 less its final byte.
        (bytes.fromhex("000a0801120600000d0ac8"), 1),
    ],
    ids=["separator-then-partial", "noise-then-partial"],
)
def test_the_line_parser_never_gets_a_frame_protobuf_is_waiting_on(data, waiting_after) -> None:
    assert _composite().parse_messages(data) == ([], waiting_after)


@pytest.mark.parametrize("reply, text", [(b"0\r\n", "0"), (b"1\r\n", "1"), (b"42\r\n", "42"), (b"OK\r\n", "OK")])
def test_a_short_scpi_reply_is_returned_whole(reply, text) -> None:
    messages, consumed = _composite().parse_messages(reply)
    assert [m.data for m in messages] == [text]
    assert consumed == len(reply)


def test_a_real_frame_arriving_in_chunks_waits() -> None:
    frame = _frame(DaqifiOutMessage(
        analog_in_port_num=16, device_sn=1234, device_pn="Nq1",
        analog_in_cal_m=[1.0] * 16, analog_in_cal_b=[0.5] * 16,
    ))
    parser = _protobuf()
    for cut in range(3, len(frame), 7):
        assert parser.parse_messages(frame[:cut]) == ([], 0), cut
    messages, consumed = parser.parse_messages(frame)
    assert len(messages) == 1 and consumed == len(frame)


def test_scpi_text_is_parsed_as_text() -> None:
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
