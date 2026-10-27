"""#164 item 3: a frame over 1 KiB whose read ends right after its length
prefix was destroyed: the gap gate skipped the prefix as garbage and the body
decoded as junk frames. An SD log whose 64 KiB read boundary landed there
failed every time; that is fixed. The live stream keeps the gap gate (see
ProtobufMessageParser.parse_messages): no byte-level rule tells a real split
frame from a bogus prefix without either destroying the first or letting the
second hold the stream (audit rounds 2-3 on #172), so the rare live case
stays as it was on main."""

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


def _feed(chunks, **kwargs):
    parser = ProtobufMessageParser(DaqifiOutMessage.FromString)
    buffer = bytearray()
    out = []
    for chunk in chunks:
        buffer += chunk
        messages, consumed = parser.parse_messages(bytes(buffer), **kwargs)
        out += [m.data for m in messages]
        del buffer[:consumed]
    return out


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


def test_sd_log_survives_every_read_boundary_inside_a_large_frame() -> None:
    frame = _big_frame()
    small = DaqifiOutMessage(msg_time_stamp=7, analog_in_data=[1, 4, 1, 1, 4, 1, 99]).SerializeToString()
    log = (_VarintBytes(len(small)) + small) * 3 + frame + (_VarintBytes(len(small)) + small) * 3
    for size in range(5, 64):
        messages = _read_all_messages(io.BytesIO(log), SdCardParseOptions(buffer_size=size))
        assert sum(_is_big(m) for m in messages) == 1, f"buffer_size={size}"
        assert sum(m.msg_time_stamp == 7 for m in messages) == 6, f"buffer_size={size}"


def test_waiting_on_a_bare_prefix_keeps_the_frame() -> None:
    frame = _big_frame()
    out = _feed([frame[:2], frame[2:]], wait_on_bare_prefix=True)
    assert len(out) == 1 and _is_big(out[0])


def test_the_live_default_still_applies_the_gap_gate() -> None:
    # Pre-existing on main and kept, documented above: the bare prefix of a
    # >1 KiB frame is skipped by default.
    parser = ProtobufMessageParser(DaqifiOutMessage.FromString)
    assert parser.parse_messages(_big_frame()[:2])[1] == 1


# Audit round 2 on #172: a bogus prefix must not hold the live stream.
_CAPTURED = bytes.fromhex("1208c6ecd8e3041201002a020000aa0202ffff")  # firmware capture
_SAMPLE = bytes.fromhex("1008071a0c0000803f0000004000004040")  # timestamp 7, 3 floats


def test_bogus_prefix_before_captured_frames_does_not_hold_the_stream() -> None:
    out = _feed([bytes([0xFF, 0x1F])] + [_CAPTURED] * 10)
    assert len(out) == 10


def test_bogus_prefix_before_sample_frames_does_not_hold_the_stream() -> None:
    out = _feed([bytes([0xFE, 0x0A]), _SAMPLE, _SAMPLE])
    assert [m.msg_time_stamp for m in out] == [7, 7]
