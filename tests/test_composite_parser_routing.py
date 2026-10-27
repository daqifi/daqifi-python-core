"""#164 items 4-5: CompositeMessageParser routing (port of upstream #457/#810)."""

import pytest

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.communication import CompositeMessageParser


def _parser():
    return CompositeMessageParser(protobuf_factory=DaqifiOutMessage.FromString)


def _feed(chunks):
    parser = _parser()
    buffer = bytearray()
    out = []
    for chunk in chunks:
        buffer += chunk
        while buffer:  # as the stream consumer does: parse until no progress
            messages, consumed = parser.parse_messages(bytes(buffer))
            out += [m.data for m in messages]
            del buffer[:consumed]
            if not messages and not consumed:
                break
    return out


def _frame(**fields) -> bytes:
    body = DaqifiOutMessage(**fields).SerializeToString()
    return bytes([len(body)]) + body


def test_a_frame_ending_in_crlf_is_parsed_as_protobuf() -> None:
    # Item 4: digital mask 0x0A0D serializes to a frame ending 0d0a, which
    # the trailing-line-ending heuristic sent to the text parser.
    frame = _frame(msg_time_stamp=1, digital_data=b"\r\n")
    assert frame.hex() == "0608012a020d0a"
    out = _feed([frame])
    assert len(out) == 1 and out[0].msg_time_stamp == 1 and out[0].digital_data == b"\r\n"


def test_three_frames_the_last_ending_in_crlf_are_all_kept() -> None:
    frames = _frame(msg_time_stamp=1) + _frame(msg_time_stamp=2) + _frame(msg_time_stamp=3, digital_data=b"\r\n")
    assert [m.msg_time_stamp for m in _feed([frames])] == [1, 2, 3]


@pytest.mark.parametrize("first", [b"0,", b"0,\"No", b"-200,\"Exec", b"SYST:ERR? 0"])
def test_a_scpi_reply_split_across_reads_keeps_its_first_bytes(first) -> None:
    # Item 5: the protobuf fallback "resynced" over the start of a partial
    # line and that progress was committed, so b'0,' + b'"No error"' came
    # out as ',"No error"'.
    full = first + b' rest"\r\n'
    assert _feed([first, full[len(first):]]) == [full[:-2].decode()]


def test_text_lines_and_scpi_echo_still_route_to_text() -> None:
    assert _feed([b'0,"No error"\r\n']) == ['0,"No error"']
    assert _feed([b"*IDN?\r\nDAQiFi,Nq1,1,01-02\r\n"]) == ["*IDN?", "DAQiFi,Nq1,1,01-02"]


def test_a_null_heavy_status_frame_with_an_echoed_command_routes_to_protobuf() -> None:
    # Upstream #268: echoed "SYST..." text before a binary status frame
    # terminated by CRLF; null density decides.
    frame = _frame(device_sn=0, analog_in_data=[0] * 40, device_pn="Nq1")
    out = _feed([frame + b"\r\n"])
    assert any(getattr(m, "device_pn", None) == "Nq1" for m in out)


# Audit round 1 on #177: the routing change must not break what the existing
# heuristics got right.

def test_a_binary_frame_with_an_embedded_crlf_and_some_nulls_stays_protobuf() -> None:
    frame = bytes.fromhex("210820120d00000d0a2828282828282828282a024142320220204020482050325828")
    out = _feed([frame])
    assert len(out) == 1 and out[0].msg_time_stamp == 32


def test_a_mostly_printable_analog_frame_with_tabs_stays_protobuf() -> None:
    frame = _frame(
        msg_time_stamp=1,
        analog_in_data_float=[0] + [2.1446077823638916] * 15,
        digital_data=b"\r\n",
        device_status=1,
    )
    out = _feed([frame])
    assert len(out) == 1 and len(out[0].analog_in_data_float) == 16


def test_a_blank_line_before_a_frame_does_not_strand_it() -> None:
    out = _feed([b"\r\n", bytes([2, 0x08, 0x01])])
    assert [m.msg_time_stamp for m in out] == [1]


def test_a_utf8_text_line_is_kept_whole() -> None:
    assert _feed(["µV\r\n".encode("utf-8")]) == ["µV"]


# Audit round 2 on #177: a lenient protobuf decode must not override text.

def test_a_numeric_reply_after_a_blank_line_stays_text() -> None:
    assert _feed([b"\r\n0,0,0,0,00\r\n"]) == ["0,0,0,0,00"]


def test_a_text_reply_before_frames_in_one_read_is_kept() -> None:
    out = _feed([b"0\r\n" + bytes([2, 0x08, 0x01]) * 20])
    assert out[0] == "0"
    assert [m.msg_time_stamp for m in out[1:]] == [1] * 20


def test_a_tab_prefixed_line_stays_text() -> None:
    assert _feed([b"\t123456789\r\n"]) == ["\t123456789"]


# Audit round 3 on #177.

def test_a_complete_frame_before_a_partial_one_is_kept() -> None:
    out = _feed([bytes.fromhex("02080102"), bytes.fromhex("0802")])
    assert [m.msg_time_stamp for m in out] == [1, 2]


def test_a_tab_prefixed_reply_that_tiles_as_a_frame_stays_text() -> None:
    assert _feed([b"\t1.23456\r\n"]) == ["\t1.23456"]
