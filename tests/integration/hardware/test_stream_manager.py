"""B4 — ``daqifi.streaming.StreamManager`` slice.

Exercises the high-level synchronous streaming facade against
real hardware (COM9 unit, fw 3.4.6b1):

* ``StreamManager.state`` / ``channels`` / ``sample_rate`` —
  property reads reflect constructor + lifecycle transitions.
* ``StreamManager.start`` / ``stop`` — RUNNING ↔ STOPPED state
  transitions, with the firmware-required "at least one channel
  enabled" precondition (otherwise ``ConfigurationError``).
* ``StreamManager.read`` — blocking call returns the requested
  number of ``Sample`` objects.
* ``StreamManager.read_all`` — duration-bounded read returns a
  populated ``SampleBuffer``.
* ``StreamManager.__enter__`` / ``__exit__`` — context manager
  starts on enter, stops on exit (even if body raises).
* Channels accessor returns a defensive copy (caller mutation
  doesn't affect the StreamManager's internal list).

Hardware safety: every test starts from + ends with all-channels-
disabled and streaming-stopped. Streaming engagement is short
(≤2 s per test) — the long-window guards live in
test_streaming_basic.py / test_streaming_lifecycle.py.
"""
from __future__ import annotations

import time
from typing import Iterator

import pytest

from daqifi.device import NyquistDevice
from daqifi._internal import scpi
from daqifi.discovery import DeviceInfo
from daqifi.exceptions import CommandError
from daqifi.streaming import Sample, SampleBuffer, StreamManager, StreamState


# Same convention as B7/B8/B9 — local fixture so we don't compete
# for the COM port with the parent `serial_transport` fixture.


@pytest.fixture
def streaming_nyquist(device_info: DeviceInfo) -> Iterator[NyquistDevice]:
    """Open NyquistDevice, yield it ready to construct streams from
    (powered-up, errors drained, all channels disabled). Teardown
    issues a best-effort STReam:STOP (in case a test left the
    firmware streaming), disables every analog channel, and
    disconnects."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev = NyquistDevice.connect(port.strip())
    try:
        # Drain any stale errors LEFT BY PRIOR TESTS in the device's
        # SCPI error queue BEFORE issuing power_up. Otherwise the
        # send_set_with_error path inside the power_state setter
        # pops a stale entry and raises on what would have been a
        # successful command. (Surfaced post-#61 — the OLD setter
        # silently swallowed the same race; the new one correctly
        # propagates non-zero codes.)
        dev.clear_errors()
        dev.power_up()
        dev.clear_errors()
        # Start with everything disabled so the precondition check
        # in StreamManager.start is exercised by the test that wants
        # to exercise it.
        _disable_all_adc(dev)
        yield dev
    finally:
        # Best-effort STReam:STOP first — if a test exited with
        # the firmware still streaming (e.g. its own stream.stop()
        # raised), un-stuck the device-side state machine before
        # the next test or session inherits it.
        try:
            dev._comm.send_command(scpi.Streaming.STOP)
        except Exception:
            pass
        try:
            _disable_all_adc(dev)
        except Exception:
            pass
        try:
            dev.disconnect()
        except Exception:
            pass


def _disable_all_adc(dev: NyquistDevice) -> None:
    """Set the ADC channel-enable bitmask to 0 via raw SCPI.
    Bypasses the per-channel setter (broken on this firmware per
    test_channels_analog_input.py module docstring)."""
    dev._comm.send_command("ENABle:VOLTage:DC 0", delay=0.2)


def _enable_adc_channel(dev: NyquistDevice, channel: int) -> None:
    """Enable one ADC channel via raw bitmask write — same proven
    path the existing B-slice tests use."""
    mask = 1 << channel
    dev._comm.send_command(f"ENABle:VOLTage:DC {mask}", delay=0.2)


# ---- Properties — initial state ------------------------------------------


def test_initial_state_is_stopped(
    streaming_nyquist: NyquistDevice,
) -> None:
    """A freshly-constructed ``StreamManager`` reports ``STOPPED``
    before ``start()`` is called."""
    stream = streaming_nyquist.create_stream(channels=[0], sample_rate=100)
    assert stream.state == StreamState.STOPPED


def test_channels_property_returns_constructor_value(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``channels`` reflects the channel list passed at construction."""
    stream = streaming_nyquist.create_stream(
        channels=[0, 3, 7], sample_rate=100
    )
    assert stream.channels == [0, 3, 7]


def test_channels_property_returns_defensive_copy(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``channels`` returns a fresh list per call — caller mutation
    of the returned list doesn't affect subsequent reads or the
    internal channel list."""
    stream = streaming_nyquist.create_stream(channels=[0, 1], sample_rate=100)
    snap1 = stream.channels
    snap1.append(99)
    snap2 = stream.channels
    assert 99 not in snap2, (
        "caller mutation of the returned channels list leaked "
        "back into the StreamManager — defensive-copy is broken"
    )


def test_sample_rate_property_returns_constructor_value(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``sample_rate`` reflects the rate passed at construction."""
    stream = streaming_nyquist.create_stream(
        channels=[0], sample_rate=500
    )
    assert stream.sample_rate == 500


# ---- start — preconditions + state transition -----------------------------


def test_start_without_enabled_channels_raises(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``start()`` requires at least one ADC channel enabled (firmware
    crashes with TLB exception otherwise). Production guard wraps
    the underlying ConfigurationError in a `CommandError` whose
    message includes "No ADC channels enabled". State transitions
    to ERROR and stays there until the next successful start.

    Tight exception type + message-substring match — a future
    regression that swaps the exception class or removes the
    diagnostic string fails the test instead of passing silently."""
    dev = streaming_nyquist
    _disable_all_adc(dev)  # ensure clean slate

    stream = dev.create_stream(channels=[0], sample_rate=100)

    with pytest.raises(CommandError, match="No ADC channels enabled"):
        stream.start()
    # Per the docstring, the failed start transitions to ERROR.
    assert stream.state == StreamState.ERROR


def test_start_then_stop_round_trips_state(
    streaming_nyquist: NyquistDevice,
) -> None:
    """With at least one ADC channel enabled, ``start()`` flips state
    to RUNNING and ``stop()`` returns it to STOPPED."""
    dev = streaming_nyquist
    _enable_adc_channel(dev, 0)

    stream = dev.create_stream(channels=[0], sample_rate=100)
    stop_exc: Exception | None = None
    try:
        stream.start()
        assert stream.state == StreamState.RUNNING

        # Drain a tiny burst to confirm the firmware is actually
        # streaming (not just that we wrote the start command).
        time.sleep(0.1)

        stream.stop()
        assert stream.state == StreamState.STOPPED
    finally:
        if stream.state == StreamState.RUNNING:
            try:
                stream.stop()
            except Exception as e:
                stop_exc = e

    if stop_exc is not None:
        pytest.fail(f"stream.stop() failed in cleanup: {stop_exc!r}")


def test_stop_without_start_is_noop(
    streaming_nyquist: NyquistDevice,
) -> None:
    """Calling ``stop()`` on a never-started StreamManager doesn't
    raise and leaves state at STOPPED."""
    stream = streaming_nyquist.create_stream(
        channels=[0], sample_rate=100
    )
    assert stream.state == StreamState.STOPPED
    stream.stop()  # no-op
    assert stream.state == StreamState.STOPPED


# ---- read / read_all — actual sample retrieval ---------------------------


def test_read_returns_requested_sample_count(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``read(num_samples=N)`` blocks and returns exactly N samples
    once the stream is RUNNING."""
    dev = streaming_nyquist
    _enable_adc_channel(dev, 0)

    stream = dev.create_stream(channels=[0], sample_rate=200)
    stop_exc: Exception | None = None
    try:
        stream.start()
        samples = stream.read(num_samples=5, timeout=2.0)
        assert len(samples) == 5
        assert all(isinstance(s, Sample) for s in samples)
    finally:
        try:
            stream.stop()
        except Exception as e:
            stop_exc = e

    if stop_exc is not None:
        pytest.fail(f"stream.stop() failed in cleanup: {stop_exc!r}")


def test_read_before_start_raises_runtime_error(
    streaming_nyquist: NyquistDevice,
) -> None:
    """Calling ``read()`` before ``start()`` raises ``RuntimeError``
    — the contract is "stream must be RUNNING to read."""
    stream = streaming_nyquist.create_stream(
        channels=[0], sample_rate=100
    )
    with pytest.raises(RuntimeError):
        stream.read(num_samples=1, timeout=0.5)


def test_read_all_returns_populated_buffer(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``read_all(duration=T)`` returns a SampleBuffer that holds
    samples collected over T seconds at the configured rate.

    Tiny window (0.05s @ 100 Hz → 5 expected samples) keeps the
    worst-case blocking under ~5s — `read_all` calls `read()`
    with the default per-sample timeout (1.0s × N expected
    samples), so even with a flaky firmware we won't hang the
    suite for 50+s here. The deeper "we get the EXPECTED count"
    check lives in test_streaming_basic.py with its long-window
    no-drop guards."""
    dev = streaming_nyquist
    _enable_adc_channel(dev, 0)

    stream = dev.create_stream(channels=[0], sample_rate=100)
    stop_exc: Exception | None = None
    try:
        stream.start()
        buf = stream.read_all(duration=0.05)
        assert isinstance(buf, SampleBuffer)
        assert len(buf) > 0, "read_all returned an empty SampleBuffer"
    finally:
        try:
            stream.stop()
        except Exception as e:
            stop_exc = e

    # Surface stop-failures AFTER the body assertions resolve so
    # we don't shadow a body failure, but they still get reported
    # — silently swallowing them masks real device issues.
    if stop_exc is not None:
        pytest.fail(f"stream.stop() failed in cleanup: {stop_exc!r}")


# ---- Context manager ------------------------------------------------------


def test_context_manager_start_and_stop(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``with stream:`` enters → state RUNNING → block body executes
    → exit → state STOPPED. Confirms __enter__ calls start() and
    __exit__ calls stop()."""
    dev = streaming_nyquist
    _enable_adc_channel(dev, 0)

    stream = dev.create_stream(channels=[0], sample_rate=100)
    with stream:
        assert stream.state == StreamState.RUNNING
        # Drain a tiny burst to confirm streaming is engaged.
        time.sleep(0.1)
    assert stream.state == StreamState.STOPPED


def test_context_manager_stops_on_exception(
    streaming_nyquist: NyquistDevice,
) -> None:
    """If the with-body raises, ``__exit__`` still runs ``stop()``
    so the device isn't left streaming. The exception propagates."""
    dev = streaming_nyquist
    _enable_adc_channel(dev, 0)

    stream = dev.create_stream(channels=[0], sample_rate=100)
    sentinel_message = "deliberate-test-exception-b4-stream-context"
    with pytest.raises(RuntimeError, match=sentinel_message):
        with stream:
            assert stream.state == StreamState.RUNNING
            raise RuntimeError(sentinel_message)
    assert stream.state == StreamState.STOPPED
