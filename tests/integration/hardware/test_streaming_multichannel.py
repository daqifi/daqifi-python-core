"""B5 — Multi-channel ADC streaming hardware tests.

Companion to ``test_streaming_basic.py`` (B4). Where B4 pins single-
channel behaviour, this slice walks the channel-bitmask axis and
asserts the per-frame *shape* invariant: every streaming frame must
carry exactly ``popcount(bitmask)`` analog samples.

The 0xFFFF (all 16 ADCs) case is the slice's headline focus. A
diagnostic run against the bench unit
fw 3.4.6b1 saw ~16 % of length-prefix-passed payloads fail to decode as
:class:`DaqifiOutMessage` at this bitmask, an order of magnitude worse
than the single-channel case. The all-16 test asserts a 5 % decode-
failure **ceiling** computed over protobuf-framed payloads only — low
enough that the historical ~16 % rate fails loudly, high enough to
tolerate ~1 % CDC scheduling jitter without flaking.

Sample-rate cap: ``scpi_commands.start_streaming`` enforces 1-1000 Hz
(per PR #17 pass 6 #5). This slice stays at 1 kHz for that reason; the
faster rates the validation script exercises (5/10 kHz) are bypassing
the typed factory and belong in a future raw-SCPI slice.
"""
from __future__ import annotations

import time
from typing import List

import pytest

from daqifi._internal import scpi_commands
from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.communication import (
    ProtobufMessageParser,
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
    """Drives the production framing path at the byte level so the
    decode-failure rate is observable.

    Uses production framing (:class:`ProtobufMessageParser` and
    :class:`ProtobufMessageParser`) but a raw-bytes ``protobuf_factory``
    that never raises, so every length-prefix-passed payload reaches
    the handler — the handler then attempts the
    :meth:`DaqifiOutMessage.FromString` decode under its own guard.
    That deviation from the production stack is deliberate: the
    production factory raises on bad payloads, which triggers
    :class:`ProtobufMessageParser`'s 1-byte resync path and hides the
    failure from the consumer. Counting failures requires letting
    them through. The trade-off is that this harness measures the
    *content-decode* rate, not the *post-resync framing* rate the
    production stack would expose.

    :class:`ProtobufMessageParser` can route a chunk through either
    its protobuf parser (payload type ``bytes`` / ``bytearray``) or
    its text parser (payload type ``str`` — SCPI replies, banner
    text, the ``DAQIFI>`` prompt). The handler counts the two
    populations separately:

    * ``protobuf_frames``     — payloads emitted by the protobuf parser
    * ``decoded_messages``    — protobuf payloads that decoded cleanly
    * ``decode_errors``       — protobuf payloads that did NOT decode
    * ``text_messages``       — payloads emitted by the text parser

    ``decode_errors / protobuf_frames`` is the metric the all-16 test
    asserts on. Text leakage (``text_messages > 0``) would skew the
    metric if folded in, so the test should also assert it stays at
    zero — the ``clean_device`` fixture and the ``_stream_for`` init
    sequence force a steady-state where only protobuf frames flow.

    For successfully-decoded protobuf frames the handler also tallies
    ``len(out.analog_in_data)`` so the popcount invariant can be
    asserted without conflating it with the decode-failure population.
    """

    def __init__(self, serial_obj) -> None:
        self.protobuf_frames = 0
        self.decoded_messages = 0
        self.decode_errors = 0
        self.text_messages = 0
        self.analog_sample_counts: List[int] = []

        # Raw-bytes factory: lets bad payloads through to the handler
        # so we can count decode failures explicitly. Using
        # ``DaqifiOutMessage.FromString`` here would silently drop
        # them inside the parser's try/except and hide the rate.
        parser = ProtobufMessageParser(lambda payload: bytes(payload)
        )
        self.consumer: StreamMessageConsumer = StreamMessageConsumer(
            stream=serial_obj, message_parser=parser
        )
        self.consumer.add_message_received(self._on_message)

    def _on_message(self, args) -> None:
        msg = getattr(args, "message", None)
        payload = getattr(msg, "data", None) if msg is not None else None
        if isinstance(payload, str):
            # Routed through LineBasedMessageParser — SCPI text leaked
            # into the stream window. Counted separately so it can't
            # skew the protobuf-decode metric.
            self.text_messages += 1
            return
        if not isinstance(payload, (bytes, bytearray)):
            # Defensive: an unexpected payload type isn't a protobuf
            # frame either, so don't fold it into the decode metric.
            return
        self.protobuf_frames += 1
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
        f"(protobuf_frames={harness.protobuf_frames}, "
        f"decode_errors={harness.decode_errors}, "
        f"text_messages={harness.text_messages})"
    )

    # No SCPI text should be flowing during a stream when the
    # ``clean_device`` fixture + ``_stream_for`` init have done their
    # job (echo off, protobuf format set). If text leaks in, the
    # decode-rate metric in the all-16 test would silently shift; gate
    # against it here too.
    assert harness.text_messages == 0, (
        f"unexpected text messages on the stream "
        f"({harness.text_messages}) — echo or stream-format may not "
        f"have been applied"
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
    a 5 s @ 1 kHz diagnostic run (bitmask
    0xFFFF) on the bench unit, fw 3.4.6b1, saw ~16 % of length-prefix-passed
    payloads fail :meth:`DaqifiOutMessage.FromString`. That rate is
    much higher than the single-channel baseline (effectively 0 %)
    and is the slice's "investigate or file a bug" target.

    The 5 % bound here is a deliberate compromise:

    * Tight enough that the historical ~16 % diagnostic rate fails the
      test loudly — if the rate ever returns to that level the suite
      will surface it instead of silently passing. (The bound was set
      after a clean bench run confirmed the production parser path
      actually delivers ~0 % when paired with the wake/echo/format
      init the ``clean_device`` fixture now covers — see PR #28 Qodo
      bug-1 fix.)
    * Loose enough that a one-off ~1 % blip from CDC scheduling jitter
      doesn't make the test flaky on the developer rig.

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

    # Text leakage would skew ``decode_failure_pct`` if folded into the
    # denominator; this ensures the metric below is over protobuf
    # payloads only.
    assert harness.text_messages == 0, (
        f"unexpected text messages on the stream "
        f"({harness.text_messages}) — would skew the decode metric"
    )
    assert harness.protobuf_frames > 0, (
        "no protobuf frames reached the handler"
    )
    decode_failure_pct = (
        100.0 * harness.decode_errors / harness.protobuf_frames
    )
    assert decode_failure_pct < 5.0, (
        f"multi-channel decode-failure rate {decode_failure_pct:.1f}% "
        f"({harness.decode_errors}/{harness.protobuf_frames}) — at or above "
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
# decimal digit strings only (#224). The firmware parses the mask as a
# decimal integer, so a hex literal such as ``FFFF`` is refused on the host
# rather than sent; the rejection test below asserts that refusal.


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
    with pytest.raises(ValueError, match="decimal bitmask string"):
        scpi_commands.enable_adc_channels("not-a-number")
