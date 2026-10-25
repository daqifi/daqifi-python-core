"""Pin DaqifiOutMessage field NUMBERS to what the Nyquist firmware emits.

Firmware #730 (daqifi-nyquist-firmware PR #733) put the streaming timebase on
fields 70-72. This repo's .proto declared six client-side ``cap_*`` fields on
70-75 instead. All are uint32, so both schemas decode cleanly and nothing
objects: a real SYSInfoPB frame reported ``cap_version = 10_500_000``, which
is ``stream_timer_freq`` in Hz.

These tests build the wire bytes BY HAND from tag numbers, never through the
generated class, so a renumbering in the .proto cannot make them agree with
themselves.
"""
from __future__ import annotations

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _uint32_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)  # wire type 0


# Values as a shipped NQ1 reports them (stream_timer_freq measured off real
# hardware; ticks/rate for a configured 1 kHz stream).
_FIRMWARE_TIMEBASE = {70: 10_500_000, 71: 42_000, 72: 1_000_000}


def test_firmware_timebase_fields_decode_by_number() -> None:
    wire = b"".join(_uint32_field(n, v) for n, v in _FIRMWARE_TIMEBASE.items())
    msg = DaqifiOutMessage.FromString(wire)

    assert msg.stream_timer_freq == 10_500_000
    assert msg.timestamp_ticks_per_sample == 42_000
    assert msg.actual_rate_millihz == 1_000_000
    # The defect: these used to absorb the timebase values.
    assert msg.cap_version == 0
    assert msg.cap_max_freq_hz == 0
    assert msg.cap_isr_max_hz == 0


def test_field_numbers_match_firmware_contract() -> None:
    by_name = {f.name: f.number for f in DaqifiOutMessage.DESCRIPTOR.fields}
    assert by_name["stream_timer_freq"] == 70
    assert by_name["timestamp_ticks_per_sample"] == 71
    assert by_name["actual_rate_millihz"] == 72
    assert [by_name[n] for n in (
        "cap_version", "cap_max_freq_hz", "cap_isr_max_hz",
        "cap_type1_agg_hz", "cap_tick_budget", "cap_tick_overhead",
    )] == [80, 81, 82, 83, 84, 85]


def test_no_client_field_squats_the_firmware_growth_range() -> None:
    """73-79 are left for the firmware's timebase block to grow into."""
    squatters = [f.name for f in DaqifiOutMessage.DESCRIPTOR.fields
                 if 73 <= f.number <= 79]
    assert squatters == []
