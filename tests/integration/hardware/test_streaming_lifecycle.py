"""B6 — Streaming lifecycle hardware tests.

Covers the start/stop/restart contract on:

* :class:`DaqifiStreamingDevice` — high-level facade properties
  (``is_streaming``, ``streaming_frequency``, ``is_usb_connection``,
  ``is_logging_to_sd_card``, ``sd_card_files``) and method lifecycle
  (``start_streaming`` / ``stop_streaming`` round-trip, restart cycles,
  idempotent stop)
* :class:`StreamMessageConsumer` — deferred symbols from B4
  (``is_running`` property, ``clear_buffer``, ``add_error_occurred`` /
  ``remove_error_occurred``)

Plus the firmware-side invariant: N start/stop cycles must not leak
samples (``QueueDroppedSamples`` and ``UsbDroppedBytes`` stay 0).

The high-level facade tests open + close their own device through the
factory rather than borrowing the ``device`` fixture (which is a
``NyquistDevice``). The firmware-stats and consumer-internals tests
borrow the ``clean_device`` / ``serial_transport`` fixtures so they
can drive the protocol at the byte level.
"""
from __future__ import annotations

import time
from typing import Any, List, Tuple

import pytest

from daqifi._internal import scpi_commands
from daqifi._internal.scpi import DeviceState
from daqifi.communication import (
    CompositeMessageParser,
    StreamMessageConsumer,
)
from daqifi.devices import (
    DaqifiDeviceFactory,
    DaqifiStreamingDevice,
)
from daqifi.discovery import DeviceInfo


# ---- Helpers ---------------------------------------------------------------


def _send(serial_obj, line: str, delay: float = 0.0) -> None:
    serial_obj.write((line + "\r").encode("ascii"))
    try:
        serial_obj.flush()
    except Exception:
        pass
    if delay > 0:
        time.sleep(delay)


def _drain(serial_obj, timeout: float) -> bytes:
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


def _open_factory_device(port: str) -> DaqifiStreamingDevice:
    """Connect via the factory using the same defaults as the B2 slice
    (port-name normalised; explicit name; baud 115200; init enabled).

    Mirrors :func:`tests/integration/hardware/test_devices_factory._open_factory_device`
    so behaviour stays consistent across slices that exercise the
    factory path."""
    return DaqifiDeviceFactory.connect_serial(
        port_name=port.strip(),
        baud_rate=115200,
        device_name="B6-test-device",
    )


# ---- DaqifiStreamingDevice — properties ------------------------------------


def test_streaming_frequency_default_and_setter(
    device_info: DeviceInfo,
) -> None:
    """``streaming_frequency`` defaults to 100 Hz, accepts positive
    ints, and rejects zero / negative.

    NB: the setter does NOT currently reject ``bool`` (a Python int
    subclass) the way ``scpi_commands.start_streaming`` does. This
    asymmetry is intentionally NOT exercised here — locking the loose
    behavior in via assertion would make a future tightening of the
    facade harder. Tracked as a follow-up for the production code, not
    something this tests-only PR should reach into."""
    dev = _open_factory_device(device_info.port)
    try:
        # Default per :class:`DaqifiStreamingDevice.__init__`.
        assert dev.streaming_frequency == 100

        dev.streaming_frequency = 250
        assert dev.streaming_frequency == 250

        with pytest.raises(ValueError, match="must be positive"):
            dev.streaming_frequency = 0
        with pytest.raises(ValueError, match="must be positive"):
            dev.streaming_frequency = -100
        # Setter must not have mutated state on the failing assignments.
        assert dev.streaming_frequency == 250
    finally:
        dev.close()


def test_is_usb_connection_true_for_serial_factory(
    device_info: DeviceInfo,
) -> None:
    """A device opened via ``connect_serial`` rides a SerialStreamTransport,
    so ``is_usb_connection`` must be True."""
    dev = _open_factory_device(device_info.port)
    try:
        assert dev.is_usb_connection is True
    finally:
        dev.close()


def test_initial_sd_state_is_idle(device_info: DeviceInfo) -> None:
    """Without an explicit SD enable, ``is_logging_to_sd_card`` reads
    False and ``sd_card_files`` is an empty snapshot. The property
    returns ``tuple(...)`` so callers literally can't mutate the
    internal cache — strongest possible defensive-copy semantics."""
    dev = _open_factory_device(device_info.port)
    try:
        assert dev.is_logging_to_sd_card is False

        files = dev.sd_card_files
        assert files == ()
        assert isinstance(files, tuple)
    finally:
        dev.close()


# ---- DaqifiStreamingDevice — lifecycle -------------------------------------


def test_start_stop_flips_is_streaming(device_info: DeviceInfo) -> None:
    """``start_streaming`` flips ``is_streaming`` True; ``stop_streaming``
    flips it back. State must transition Ready → Streaming → Ready."""
    dev = _open_factory_device(device_info.port)
    try:
        # Bias to a low rate so a tight start/stop window does not flood
        # the consumer.
        dev.streaming_frequency = 100

        assert dev.is_streaming is False
        assert dev.state == DeviceState.Ready

        dev.start_streaming()
        try:
            assert dev.is_streaming is True
            assert dev.state == DeviceState.Streaming
            # Brief settle so the firmware actually engages streaming
            # before we tear it down — keeps the SCPI error queue clean.
            time.sleep(0.3)
        finally:
            dev.stop_streaming()

        assert dev.is_streaming is False
        # State must drop back to Ready (not stay at Streaming).
        assert dev.state == DeviceState.Ready
    finally:
        dev.close()


def test_stop_streaming_is_idempotent(device_info: DeviceInfo) -> None:
    """Calling ``stop_streaming`` when nothing is streaming, and
    repeatedly after a real stop, must not raise. ``is_streaming``
    stays False across the calls and the device state never advances
    past Ready."""
    dev = _open_factory_device(device_info.port)
    try:
        # Cold stop — never started.
        assert dev.is_streaming is False
        dev.stop_streaming()
        assert dev.is_streaming is False
        assert dev.state == DeviceState.Ready

        # Start + stop, then double-stop.
        dev.streaming_frequency = 100
        dev.start_streaming()
        time.sleep(0.2)
        dev.stop_streaming()
        assert dev.is_streaming is False
        dev.stop_streaming()
        dev.stop_streaming()
        assert dev.is_streaming is False
        assert dev.state == DeviceState.Ready
    finally:
        dev.close()


def test_start_stop_restart_cycle_through_facade(
    device_info: DeviceInfo,
) -> None:
    """The facade must support multiple start/stop cycles back to back
    without internal state corruption: ``is_streaming`` toggles correctly
    each cycle and the device stays connected."""
    dev = _open_factory_device(device_info.port)
    try:
        dev.streaming_frequency = 200
        for _ in range(3):
            assert dev.is_streaming is False
            dev.start_streaming()
            assert dev.is_streaming is True
            time.sleep(0.25)
            dev.stop_streaming()
            assert dev.is_streaming is False
            # Settle window between cycles so the firmware finishes the
            # previous stop transition before we issue the next start.
            time.sleep(0.1)
        assert dev.is_connected is True
    finally:
        dev.close()


# ---- Firmware-side: N restart cycles must not leak samples -----------------


def test_n_restart_cycles_no_firmware_drops(
    clean_device, firmware_stats_diff
) -> None:
    """Five consecutive 200 Hz x 0.4 s start/stop cycles via the SCPI
    layer must leave every firmware drop counter at zero
    (``UsbDroppedBytes``, ``QueueDroppedSamples``, etc.). This is the
    load-bearing "no leaked samples across N restarts" check from the
    B6 plan.

    Drives the device through the SCPI layer (rather than the facade)
    so we can wrap the whole cycle batch in a single
    ``firmware_stats_diff`` window — the facade owns its own consumer
    that would race the stats fixture's serial reads.

    NB: ``TotalSamplesStreamed`` is *not* asserted on here. B4 already
    documents that this counter is order-sensitive on COM7 fw 3.4.6b1
    (it under-reports across back-to-back start/stop cycles, and on
    the cycle-batch path observed during B6 development it can even
    snapshot to a value below the pre-snapshot — likely a 32-bit
    rollover sampled mid-update). Drop counters are the contract
    "no leaked samples" actually rests on."""
    serial_obj = clean_device.stream
    cycles = 5
    rate_hz = 200
    duration_s = 0.4

    _send(serial_obj, scpi_commands.disable_device_echo().data, delay=0.1)
    _drain(serial_obj, timeout=0.2)
    _send(serial_obj, scpi_commands.set_protobuf_stream_format().data, delay=0.1)
    _drain(serial_obj, timeout=0.2)
    _send(serial_obj, scpi_commands.enable_adc_channels("1").data, delay=0.2)
    _drain(serial_obj, timeout=0.2)

    firmware_stats_diff.open()
    try:
        for _ in range(cycles):
            _send(serial_obj, scpi_commands.start_streaming(rate_hz).data)
            time.sleep(duration_s)
            _send(serial_obj, scpi_commands.stop_streaming().data, delay=0.2)
            # Drain the in-flight bytes so the next cycle's firmware-side
            # buffers start fresh — without this the per-cycle queue
            # pressure compounds and a transient drop could be hidden by
            # an earlier cycle clearing.
            _drain(serial_obj, timeout=0.3)
    finally:
        # Always stop streaming + drain on the way out: if anything in
        # the cycle loop raised, leaking an active stream into the
        # ``firmware_stats_diff.close()`` snapshot would corrupt the
        # SYST:STR:STATS? read with leftover protobuf bytes (mirrors
        # the established pattern in test_phase_a_sentinel.py).
        try:
            _send(serial_obj, scpi_commands.stop_streaming().data, delay=0.3)
            _drain(serial_obj, timeout=0.3)
        except Exception:
            pass
        firmware_stats_diff.close()

    firmware_stats_diff.assert_no_drops()


# ---- StreamMessageConsumer — is_running tracks lifecycle -------------------


def test_consumer_is_running_property_tracks_lifecycle(clean_device) -> None:
    """``is_running`` is False before start, True after start, False
    after a clean stop, and survives a restart."""
    serial_obj = clean_device.stream
    parser = CompositeMessageParser(protobuf_factory=lambda b: bytes(b))
    consumer = StreamMessageConsumer(stream=serial_obj, message_parser=parser)
    try:
        assert consumer.is_running is False
        consumer.start()
        # Brief settle so the background thread is actually inside the
        # read loop before we check.
        time.sleep(0.1)
        assert consumer.is_running is True
        # Calling start again while already running is a documented
        # no-op — must not flip the property.
        consumer.start()
        assert consumer.is_running is True
        consumer.stop()
        assert consumer.is_running is False
        # Restart after stop.
        consumer.start()
        time.sleep(0.1)
        assert consumer.is_running is True
        assert consumer.stop_safely(timeout_ms=1000) is True
        assert consumer.is_running is False
    finally:
        consumer.close()


# ---- StreamMessageConsumer — clear_buffer ----------------------------------


class _NoConsumeParser:
    """Test parser that NEVER consumes bytes from the buffer.

    Forces ``StreamMessageConsumer._message_buffer`` to accumulate
    every byte that arrives so we can assert ``clear_buffer`` actually
    drops them. The real ``CompositeMessageParser`` would chew through
    streaming bytes immediately and ``queued_message_count`` would
    rarely be observably non-zero between reads."""

    def parse_messages(self, data: bytes) -> Tuple[List[Any], int]:
        return [], 0


def test_consumer_clear_buffer_drops_queued_bytes(clean_device) -> None:
    """``clear_buffer`` clears both the consumer's internal message
    buffer AND drains the stream's ``in_waiting`` residue.

    Two scenarios in one test:

    1. **Message-buffer path** — start the consumer with a parser that
       NEVER consumes. Streaming bytes accumulate in the consumer's
       ``_message_buffer``. ``clear_buffer`` drops them and shrinks
       ``queued_message_count``.
    2. **In-waiting path** — stop the consumer (so it can no longer
       drain bytes), stream briefly so pyserial's RX queue fills up,
       call ``clear_buffer``, and verify both the message buffer and
       ``in_waiting`` are empty.
    """
    serial_obj = clean_device.stream
    consumer = StreamMessageConsumer(
        stream=serial_obj, message_parser=_NoConsumeParser()
    )
    try:
        _send(
            serial_obj,
            scpi_commands.disable_device_echo().data, delay=0.1,
        )
        _send(
            serial_obj,
            scpi_commands.set_protobuf_stream_format().data, delay=0.1,
        )
        _send(
            serial_obj,
            scpi_commands.enable_adc_channels("1").data, delay=0.2,
        )

        # Scenario 1: message-buffer path. Run the consumer with a
        # parser that never consumes bytes so the internal buffer
        # accumulates. STOP streaming + the consumer thread BEFORE
        # calling ``clear_buffer`` so the post-clear check is
        # deterministic — otherwise the still-running thread can append
        # bytes between our clear and our assert, making the strict
        # equality flaky.
        consumer.start()
        try:
            _send(serial_obj, scpi_commands.start_streaming(200).data)
            # Poll up to 2.5 s for the no-consume parser to have
            # accumulated at least one read pass into the buffer. The
            # underlying pyserial.timeout is 0.5 s, so a single read
            # cycle can take that long; budgeting 2.5 s gives 5x
            # headroom for OS scheduling jitter.
            deadline = time.monotonic() + 2.5
            queued_before = 0
            while time.monotonic() < deadline:
                queued_before = consumer.queued_message_count
                if queued_before > 0:
                    break
                time.sleep(0.05)
            assert queued_before > 0, (
                "no-consume parser never accumulated bytes within 2.5 s; "
                "either streaming did not engage or the consumer thread "
                "is starved"
            )
            # Quiesce both producer (firmware) and consumer (read
            # thread) before the clear so no fresh bytes can land
            # between clear_buffer() and the post-check.
            _send(serial_obj, scpi_commands.stop_streaming().data, delay=0.3)
            assert consumer.stop_safely(timeout_ms=1000) is True
            # The consumer's stop() / stop_safely() already clears the
            # message buffer as part of shutdown — but clear_buffer()
            # under the load-bearing contract MUST hold the buffer at
            # zero for any further reads. After the explicit clear,
            # queued_message_count must be 0 deterministically (the
            # thread is no longer running, so nothing can re-fill it).
            consumer.clear_buffer()
            assert consumer.queued_message_count == 0, (
                f"clear_buffer left {consumer.queued_message_count} bytes "
                f"in the message buffer after consumer stop "
                f"(was {queued_before} before the stop+clear)"
            )
        finally:
            # Belt-and-suspenders: stop_safely above should already have
            # halted the thread; this finally only matters if the
            # earlier asserts raised before we reached it.
            consumer.stop()

        # Scenario 2: in_waiting drain. Stream with the consumer
        # stopped so bytes pile up in pyserial's OS RX queue; then
        # clear_buffer must drain them.
        _send(serial_obj, scpi_commands.start_streaming(200).data)
        time.sleep(0.4)
        _send(serial_obj, scpi_commands.stop_streaming().data, delay=0.3)
        # in_waiting is a snapshot — read it before clear_buffer.
        in_waiting_before = serial_obj.in_waiting
        assert in_waiting_before > 0, (
            "expected pyserial RX buffer to fill while consumer was "
            "stopped; in_waiting stayed at 0"
        )
        consumer.clear_buffer()
        assert consumer.queued_message_count == 0
        assert serial_obj.in_waiting == 0, (
            f"clear_buffer left {serial_obj.in_waiting} bytes in "
            f"pyserial in_waiting (started at {in_waiting_before})"
        )
    finally:
        consumer.close()


# ---- StreamMessageConsumer — error_occurred handler round-trip -------------


class _RaisingParser:
    """Test parser that always raises. The consumer's read loop will
    route the exception to every registered error handler."""

    def parse_messages(self, data: bytes) -> Tuple[List[Any], int]:
        raise RuntimeError("intentional parser failure for B6")


def _drive_brief_streaming(serial_obj) -> None:
    """Engage 100 Hz x 0.4 s streaming so the consumer's background
    thread does at least one read pass while a test scenario is
    armed. Caller is responsible for the consumer's start/stop."""
    _send(serial_obj, scpi_commands.disable_device_echo().data, delay=0.1)
    _send(serial_obj, scpi_commands.set_protobuf_stream_format().data, delay=0.1)
    _send(serial_obj, scpi_commands.enable_adc_channels("1").data, delay=0.2)
    _send(serial_obj, scpi_commands.start_streaming(100).data)
    time.sleep(0.4)
    _send(serial_obj, scpi_commands.stop_streaming().data, delay=0.3)


def test_consumer_error_handler_fires_when_parser_raises(
    clean_device,
) -> None:
    """Subscribing via ``add_error_occurred`` then triggering a parser
    exception (the raising parser) routes the exception through the
    handler. Each error event carries the originating exception in
    ``args.error``."""
    serial_obj = clean_device.stream
    errors: List[Any] = []

    def handler(args: Any) -> None:
        errors.append(args)

    consumer = StreamMessageConsumer(
        stream=serial_obj, message_parser=_RaisingParser()
    )
    consumer.add_error_occurred(handler)
    try:
        consumer.start()
        _drive_brief_streaming(serial_obj)
        # Poll up to 2 s for the consumer thread to land at least one
        # read+parse pass and route the parser's exception through the
        # error-handler chain. Without this synchronization the test
        # races the background thread on the first read iteration
        # (whose blocking pyserial read can take up to 0.5 s).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not errors:
            time.sleep(0.05)
    finally:
        consumer.stop()
        consumer.close()
    _drain(serial_obj, timeout=0.3)

    assert errors, "error handler never fired despite raising parser"
    # The first error must wrap a RuntimeError matching our trigger so
    # we know we caught the parser failure (not some unrelated I/O
    # exception).
    first = errors[0]
    err = getattr(first, "error", None)
    assert isinstance(err, RuntimeError)
    assert "intentional parser failure for B6" in str(err)


def test_consumer_remove_error_handler_silences_callbacks(
    clean_device,
) -> None:
    """``remove_error_occurred`` unsubscribes the handler; subsequent
    parser failures must not fire it. Removing twice is a silent no-op
    (matches ``remove_message_received``'s contract)."""
    serial_obj = clean_device.stream
    errors: List[Any] = []

    def handler(args: Any) -> None:
        errors.append(args)

    consumer = StreamMessageConsumer(
        stream=serial_obj, message_parser=_RaisingParser()
    )
    consumer.add_error_occurred(handler)
    consumer.remove_error_occurred(handler)
    # Second remove is silent.
    consumer.remove_error_occurred(handler)
    try:
        consumer.start()
        _drive_brief_streaming(serial_obj)
    finally:
        consumer.stop()
        consumer.close()
    _drain(serial_obj, timeout=0.3)

    assert errors == [], (
        "removed error handler still fired "
        f"({len(errors)} event(s) leaked)"
    )
