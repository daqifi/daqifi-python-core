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
        messages, consumed = parser.parse_messages(bytes(buffer))
        out += [m.data for m in messages]
        del buffer[:consumed]
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
