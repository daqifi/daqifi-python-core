"""#164 item 3: a frame over 1 KiB whose read ends right after its length
prefix was destroyed: the parser skipped the prefix as garbage (upstream's
gap gate) and then decoded the body as junk frames."""

import io

from google.protobuf.internal.encoder import _VarintBytes

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.communication import ProtobufMessageParser
from daqifi.sdcard import SdCardParseOptions, _read_all_messages


def _big_frame() -> bytes:
    # A 1.4 KB status-like frame (a list of networks), as in the audit repro.
    message = DaqifiOutMessage(
        device_sn=123,
        av_ssid=["network_%02d" % i + "x" * 22 for i in range(40)],
    )
    body = message.SerializeToString()
    return _VarintBytes(len(body)) + body


def _is_big(message) -> bool:
    return message.device_sn == 123 and len(message.av_ssid) == 40


def _feed(chunks):
    parser = ProtobufMessageParser(DaqifiOutMessage.FromString)
    buffer = bytearray()
    out = []
    for chunk in chunks:
        buffer += chunk
        messages, consumed = parser.parse_messages(bytes(buffer))
        out += [m.data for m in messages]
        del buffer[:consumed]
    return out


def test_read_ending_right_after_the_prefix_keeps_the_frame() -> None:
    frame = _big_frame()
    assert len(frame) > 1024 + 2 and frame[1] < 0x80  # two-byte prefix
    out = _feed([frame[:2], frame[2:]])
    assert len(out) == 1 and _is_big(out[0])


def test_bare_prefix_is_not_consumed() -> None:
    parser = ProtobufMessageParser(DaqifiOutMessage.FromString)
    assert parser.parse_messages(_big_frame()[:2]) == ([], 0)


def test_garbage_after_a_bare_prefix_is_still_skipped() -> None:
    # The wait only lasts until a body byte arrives; the field-tag gate then
    # rejects an implausible one (wire type 7) and the parser resyncs.
    frame = DaqifiOutMessage(msg_time_stamp=7).SerializeToString()
    real = _VarintBytes(len(frame)) + frame
    out = _feed([bytes([0xFE, 0x0A]), bytes([0x07]) + real])
    assert [m.msg_time_stamp for m in out] == [7]


def test_sd_log_with_a_read_boundary_after_the_prefix_keeps_the_frame() -> None:
    # Place the big frame's prefix at offset 65534 so the default 64 KiB SD
    # read ends between the prefix and the body; the log failed every time.
    small = DaqifiOutMessage(msg_time_stamp=7, analog_in_data=[1, 2, 3]).SerializeToString()
    small_frame = _VarintBytes(len(small)) + small
    target = SdCardParseOptions().buffer_size - 2
    filler = small_frame * (target // len(small_frame))
    pad = target - len(filler)
    if pad:
        padding = DaqifiOutMessage(device_pn="x" * (pad - 4)).SerializeToString()
        filler += _VarintBytes(len(padding)) + padding
    assert len(filler) == target

    messages = _read_all_messages(io.BytesIO(filler + _big_frame()), SdCardParseOptions())

    assert any(_is_big(m) for m in messages)
