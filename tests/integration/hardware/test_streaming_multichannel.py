"""B5 — Multi-channel ADC streaming hardware tests.

Companion to ``test_streaming_basic.py`` (B4). Where B4 pins single-
channel behaviour, this slice walks the channel-bitmask axis and
asserts the per-frame *shape* invariant: every streaming frame must
carry exactly ``popcount(bitmask)`` analog samples.

The 0xFFFF (all 16 ADCs) case is the slice's headline focus. A
diagnostic run via ``scripts/hw_streaming_validation.py`` against COM7
fw 3.4.6b1 saw ~16 % of length-prefix-passed payloads fail to decode as
:class:`DaqifiOutMessage` at this bitmask, an order of magnitude worse
than the single-channel case. The two tests here measure that rate
through the production parser path so a regression or a fix is visible
in the suite — the all-16 test asserts a 5 % decode-failure floor (high
enough to tolerate the observed 16 % so the suite stays green, low
enough that a future parser fix that drops the rate to 0 will surface
as a tighter bound waiting to be lowered).

Sample-rate cap: ``scpi_commands.start_streaming`` enforces 1-1000 Hz
(per PR #17 pass 6 #5). This slice stays at 1 kHz for that reason; the
faster rates the validation script exercises (5/10 kHz) are bypassing
the typed factory and belong in a future raw-SCPI slice.
"""
from __future__ import annotations

import time
from typing import List, Optional

import pytest

from daqifi._internal import scpi_commands
from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.communication import (
    CompositeMessageParser,
    StreamMessageConsumer,
)


# ---- Helpers ---------------------------------------------------------------


def _send(serial_obj, line: str, delay: float = 0.0) -> None:
    """Write a CR-terminated SCPI line to the underlying pyserial."""
    serial_obj.write((line + "\r").encode("ascii"))
    try:
        serial_obj.flush()
    except Exception:
        pass
    if delay > 0:
        time.sleep(delay)


def _drain(serial_obj, timeout: float) -> bytes:
    """Drain the serial RX buffer for up to ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        try:
            n = serial_obj.in_waiting
        except Exception:
            break
        if n > 0:
            buf.extend(serial_obj.read(n))
        else:
            time.sleep(0.005)
    return bytes(buf)


def _popcount(n: int) -> int:
    """Inline popcount — Python 3.10's :meth:`int.bit_count` is the
    obvious choice but the project still supports older interpreters
    in some contexts; this loop is trivially equivalent and keeps the
    test file self-contained."""
    c = 0
    while n:
        c += n & 1
        n >>= 1
    return c


class _MultiChannelHarness:
    """Drives the production parser stack at the byte level so the
    decode rate is observable.

    The protobuf factory returns the raw payload bytes rather than
    :class:`DaqifiOutMessage`, so every successful length-prefix slice
    fires the handler. The handler then attempts the protobuf decode
    inside a guard, which lets the test distinguish three populations:

    * ``parsed_frames``       — payloads the parser handed up
    * ``decoded_messages``    — payloads that decoded as a DaqifiOutMessage
    * ``decode_errors``       — payloads that did NOT decode

    For successfully-decoded frames the handler also tallies
    ``len(out.analog_in_data)`` so the popcount invariant can be
    asserted without conflating it with the decode-failure population.
    """

    def __init__(self, serial_obj) -> None:
        self.parsed_frames = 0
        self.decoded_messages = 0
        self.decode_errors = 0
        self.analog_sample_counts: List[int] = []

        # Raw-bytes factory: lets bad payloads through to the handler
        # so we can count decode failures explicitly. Using
        # ``DaqifiOutMessage.FromString`` here would silently drop
        # them inside the parser's try/except and hide the rate.
        parser = CompositeMessageParser(
            protobuf_factory=lambda payload: bytes(payload)
        )
        self.consumer: StreamMessageConsumer = StreamMessageConsumer(
            stream=serial_obj, message_parser=parser
        )
        self.consumer.add_message_received(self._on_message)

    def _on_message(self, args) -> None:
        self.parsed_frames += 1
        msg = getattr(args, "message", None)
        payload = getattr(msg, "data", None) if msg is not None else None
        if not isinstance(payload, (bytes, bytearray)):
            self.decode_errors += 1
            return
        try:
            out = DaqifiOutMessage.FromString(bytes(payload))
        except Exception:
            self.decode_errors += 1
            return
        self.decoded_messages += 1
        self.analog_sample_counts.append(len(out.analog_in_data))


def _stream_for(
    serial_obj,
    *,
    bitmask: int,
    rate_hz: int,
    duration_s: float,
) -> _MultiChannelHarness:
    """Force protobuf framing + echo off, enable the requested ADC
    channels via ``ENAble:VOLTage:DC <bitmask>``, run the streaming
    capture, and return the populated harness.

    See :func:`tests.integration.hardware.test_streaming_basic._stream_for`
    for the rationale behind the explicit echo + format init: the
    ``clean_device`` fixture doesn't touch either, so a JSON/CSV
    stream format leaked in from a prior run would inflate
    ``decode_errors`` here in a way that has nothing to do with the
    parser.
    """
    _send(serial_obj, scpi_commands.disable_device_echo().data, delay=0.1)
    _drain(serial_obj, timeout=0.2)
    _send(serial_obj, scpi_commands.set_protobuf_stream_format().data, delay=0.1)
    _drain(serial_obj, timeout=0.2)
    _send(
        serial_obj,
        scpi_commands.enable_adc_channels(str(bitmask)).data,
        delay=0.2,
    )
    _drain(serial_obj, timeout=0.2)

    harness = _MultiChannelHarness(serial_obj)
    harness.consumer.start()
    try:
        _send(serial_obj, scpi_commands.start_streaming(rate_hz).data)
        time.sleep(duration_s)
        _send(serial_obj, scpi_commands.stop_streaming().data, delay=0.5)
        # Settle window so in-flight frames flush before stop().
        time.sleep(0.2)
    finally:
        harness.consumer.stop()
        harness.consumer.close()
    _drain(serial_obj, timeout=0.5)
    return harness


# ---- Per-frame popcount invariant across the bitmask axis ------------------


@pytest.mark.parametrize(
    "bitmask",
    [
        0x0001,  # 1 channel  — baseline alongside B4 single-channel
        0x0055,  # 4 channels (alternating) — interior shape, not full
        0x00FF,  # 8 channels — half the ADC bank
        0xFFFF,  # 16 channels — the headline B5 case
    ],
    ids=["mask_0x0001_1ch", "mask_0x0055_4ch", "mask_0x00FF_8ch", "mask_0xFFFF_16ch"],
)
def test_per_frame_analog_sample_count_matches_popcount(
    clean_device,
    firmware_stats_diff,
    bitmask: int,
) -> None:
    """Every streaming frame must carry exactly ``popcount(bitmask)``
    analog samples — that's the per-frame shape contract for the
    ADC-bank streaming format on Nyquist1.

    Cross-checks performed for every bitmask:

    * Yield floor — at least one decoded frame (the fixture would have
      failed long before this if streaming were broken).
    * Popcount invariant — every entry of ``analog_sample_counts`` equals
      ``popcount(bitmask)``. A single mis-shaped frame fails the test.
    * Firmware drop counters — :meth:`assert_no_drops` covers
      ``UsbDroppedBytes`` / ``QueueDroppedSamples`` and the rest.

    The popcount assertion lives in this test (not B4) because at
    bitmask=1 the single-channel sample count of 1 is also the default
    "looks like noise / status broadcast" value; pinning it across
    multiple bitmasks proves the relationship is causal."""
    serial_obj = clean_device.stream
    expected_per_frame = _popcount(bitmask)

    firmware_stats_diff.open()
    harness = _stream_for(
        serial_obj, bitmask=bitmask, rate_hz=1000, duration_s=2.0
    )
    firmware_stats_diff.close()

    assert harness.decoded_messages > 0, (
        f"no decoded streaming frames at bitmask=0x{bitmask:04x} "
        f"(parsed={harness.parsed_frames}, decode_errors={harness.decode_errors})"
    )

    # Every successfully-decoded frame carries popcount(bitmask) analog
    # samples. A *single* mismatch fails the test — the report calls
    # out the distinct counts seen so the diagnosis points straight at
    # the offending shape.
    distinct_counts = sorted(set(harness.analog_sample_counts))
    assert distinct_counts == [expected_per_frame], (
        f"bitmask=0x{bitmask:04x} expected {expected_per_frame} samples per "
        f"frame, got distinct counts: {distinct_counts!r} "
        f"(decoded={harness.decoded_messages})"
    )

    firmware_stats_diff.assert_no_drops()


# ---- Decode-failure rate on the all-16 bitmask -----------------------------


def test_all_sixteen_channels_decode_rate_under_5_pct(
    clean_device, firmware_stats_diff
) -> None:
    """Regression bound on the multi-channel decode-failure rate.

    Diagnostic context: a one-off run of
    ``scripts/hw_streaming_validation.py`` (5 s @ 1 kHz, bitmask
    0xFFFF) on COM7 fw 3.4.6b1 saw ~16 % of length-prefix-passed
    payloads fail :meth:`DaqifiOutMessage.FromString`. That rate is
    much higher than the single-channel baseline (effectively 0 %)
    and is the slice's "investigate or file a bug" target.

    The 5 % bound here is a deliberate compromise:

    * Loose enough that the suite stays green at the historically-
      observed ~16 % rate is **not** tolerated — if the rate stays at
      the diagnostic level, the test fails and the follow-up issue
      stays open. (Bound was set after a clean run on COM7 confirmed
      the production parser path actually delivers ~0 % when paired
      with the wake/echo/format init the ``clean_device`` fixture
      now covers — see PR #28 Qodo bug-1 fix.)
    * Tight enough that a future regression that drops 5 %+ of
      multi-channel payloads has somewhere visible to fail.

    A future PR that proves the rate is consistently ~0 % across
    multiple firmwares should tighten this bound (and probably fold
    it into the parametrized test above as a hard ``decode_errors == 0``
    assertion)."""
    serial_obj = clean_device.stream

    firmware_stats_diff.open()
    harness = _stream_for(
        serial_obj, bitmask=0xFFFF, rate_hz=1000, duration_s=5.0
    )
    firmware_stats_diff.close()

    assert harness.parsed_frames > 0, "no payloads reached the handler"
    decode_failure_pct = (
        100.0 * harness.decode_errors / harness.parsed_frames
    )
    assert decode_failure_pct < 5.0, (
        f"multi-channel decode-failure rate {decode_failure_pct:.1f}% "
        f"({harness.decode_errors}/{harness.parsed_frames}) — at or above "
        f"the historical diagnostic level. The parser may be misframing "
        f"larger protobuf payloads; see follow-up issue."
    )

    # Every successfully-decoded frame still must carry 16 samples —
    # the popcount invariant holds even when some payloads fail to
    # decode at all.
    distinct_counts = sorted(set(harness.analog_sample_counts))
    assert distinct_counts == [16], (
        f"expected 16 samples per frame at bitmask=0xFFFF, "
        f"got distinct counts: {distinct_counts!r}"
    )

    firmware_stats_diff.assert_no_drops()


# ---- Typed factory rejects bitmask shapes that would break SCPI framing ----
#
# Hex-literal note: the typed factory ``enable_adc_channels`` accepts
# decimal *and* hex digit strings on the host side, but COM7 fw 3.4.6b1
# only honours the decimal form on the device side — sending the hex
# literal ``FFFF`` enables zero channels rather than all 16. The host-
# side factory acceptance is exercised by the rejection test below;
# the firmware-side hex-literal mismatch is a documentation/firmware
# divergence to follow up on separately and is not asserted here.


def test_enable_adc_channels_factory_rejects_bad_input(device_info) -> None:
    """The typed factory rejects bitmask strings that could break SCPI
    framing or inject a second command (the hardening Qodo PR 17 pass
    7 #5 added). Running this on a hardware device gates the test on
    the real port being available, while keeping the assertion purely
    in-process — no streaming required.

    These are the exact preconditions the streaming tests above rely
    on for safe per-test bitmask injection: if a future change weakens
    the validation, this test fails before any of the parameterised
    streaming tests get the chance to send a malformed command to the
    device."""
    del device_info  # noqa: just gates the test on hardware presence

    with pytest.raises(TypeError, match="must not be None"):
        scpi_commands.enable_adc_channels(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must not be empty"):
        scpi_commands.enable_adc_channels("")
    with pytest.raises(ValueError, match="CR/LF"):
        scpi_commands.enable_adc_channels("1\r\nMEM:RES")
    with pytest.raises(ValueError, match="decimal or hex"):
        scpi_commands.enable_adc_channels("not-a-number")
