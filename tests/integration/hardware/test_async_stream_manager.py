"""B14 — ``daqifi.streaming.AsyncStreamManager`` slice.

Exercises the async streaming facade against real hardware
(COM9 unit, fw 3.4.6b1):

* ``await stream.start()`` / ``await stream.stop()`` —
  underlying ``StreamManager`` state flips to RUNNING / STOPPED.
* ``await stream.read(num_samples=N)`` — returns N ``Sample``
  instances once running.
* ``async with stream:`` — context manager starts on enter,
  stops on exit (even when the body raises).
* ``async for sample in stream:`` — iterator yields samples,
  raises ``StopAsyncIteration`` once the underlying state isn't
  RUNNING.
* ``NyquistDevice.create_stream(async_mode=True)`` — factory
  returns an ``AsyncStreamManager`` (covers the async branch
  of the factory).

Hardware safety: every test stops streaming + leaves all
channels disabled. Engagement windows are short (≤2s).
"""
from __future__ import annotations

from typing import Iterator

import pytest

from daqifi.device import NyquistDevice
from daqifi._internal import scpi
from daqifi.discovery import DeviceInfo
from daqifi.streaming import AsyncStreamManager, Sample, StreamState


# All tests in this module use asyncio. Mark them once via the
# module-level pytestmark — saves sprinkling @pytest.mark.asyncio on
# every test_* function.
pytestmark = pytest.mark.asyncio


@pytest.fixture
def streaming_nyquist(device_info: DeviceInfo) -> Iterator[NyquistDevice]:
    """Open NyquistDevice with one ADC channel enabled. Teardown
    issues a best-effort STReam:STOP (in case a test left the
    firmware streaming), disables every analog channel, and
    disconnects."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev = NyquistDevice.connect(port.strip())
    try:
        # Drain stale errors LEFT BY PRIOR TESTS BEFORE power_up so
        # the SET command's send_set_with_error doesn't pop one.
        dev.clear_errors()
        dev.power_up()
        dev.clear_errors()
        # Enable channel 0 so every test can start without first
        # hitting the precondition guard (B4 already covers that).
        dev._comm.send_command("ENABle:VOLTage:DC 1", delay=0.2)
        yield dev
    finally:
        try:
            dev._comm.send_command(scpi.Streaming.STOP)
        except Exception:
            pass
        try:
            dev._comm.send_command("ENABle:VOLTage:DC 0", delay=0.2)
        except Exception:
            pass
        try:
            dev.disconnect()
        except Exception:
            pass


# ---- Factory: NyquistDevice.create_stream(async_mode=True) ---------------


async def test_create_stream_async_returns_async_manager(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``device.create_stream(async_mode=True)`` returns an
    ``AsyncStreamManager`` instance — covers the async branch of
    the factory (the sync branch is exercised throughout B4)."""
    stream = streaming_nyquist.create_stream(
        channels=[0], sample_rate=100, async_mode=True
    )
    assert isinstance(stream, AsyncStreamManager)


# ---- start / stop — async lifecycle ---------------------------------------


async def test_async_start_then_stop_round_trips(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``await stream.start()`` flips the underlying StreamManager
    state to RUNNING; ``await stream.stop()`` returns it to
    STOPPED."""
    stream = streaming_nyquist.create_stream(
        channels=[0], sample_rate=100, async_mode=True
    )
    stop_exc: Exception | None = None
    try:
        await stream.start()
        assert stream._stream.state == StreamState.RUNNING
        await stream.stop()
        assert stream._stream.state == StreamState.STOPPED
    finally:
        # Best-effort cleanup if the body raised mid-run.
        if stream._stream.state == StreamState.RUNNING:
            try:
                await stream.stop()
            except Exception as e:
                stop_exc = e

    if stop_exc is not None:
        pytest.fail(f"async stream.stop() failed in cleanup: {stop_exc!r}")


# ---- read — async sample retrieval ----------------------------------------


async def test_async_read_returns_requested_sample_count(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``await stream.read(num_samples=N)`` returns exactly N
    ``Sample`` instances once the stream is running."""
    stream = streaming_nyquist.create_stream(
        channels=[0], sample_rate=200, async_mode=True
    )
    stop_exc: Exception | None = None
    try:
        await stream.start()
        samples = await stream.read(num_samples=5)
        assert len(samples) == 5
        assert all(isinstance(s, Sample) for s in samples)
    finally:
        try:
            await stream.stop()
        except Exception as e:
            stop_exc = e

    if stop_exc is not None:
        pytest.fail(f"async stream.stop() failed in cleanup: {stop_exc!r}")


# ---- Async context manager -----------------------------------------------


async def test_async_context_manager_start_and_stop(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``async with stream:`` enters → state RUNNING → block body
    runs → exit → state STOPPED. Confirms ``__aenter__`` calls
    ``start()`` and ``__aexit__`` calls ``stop()``."""
    stream = streaming_nyquist.create_stream(
        channels=[0], sample_rate=100, async_mode=True
    )
    async with stream:
        assert stream._stream.state == StreamState.RUNNING
        # Drain a sample to confirm streaming is engaged.
        samples = await stream.read(num_samples=1)
        assert len(samples) == 1
    assert stream._stream.state == StreamState.STOPPED


async def test_async_context_manager_stops_on_exception(
    streaming_nyquist: NyquistDevice,
) -> None:
    """If the async-with body raises, ``__aexit__`` still runs
    ``stop()`` so the device isn't left streaming. The exception
    propagates."""
    stream = streaming_nyquist.create_stream(
        channels=[0], sample_rate=100, async_mode=True
    )
    sentinel_message = "deliberate-test-exception-b14-async-context"
    with pytest.raises(RuntimeError, match=sentinel_message):
        async with stream:
            assert stream._stream.state == StreamState.RUNNING
            raise RuntimeError(sentinel_message)
    assert stream._stream.state == StreamState.STOPPED


# ---- Async iteration (async-for) -----------------------------------------


async def test_async_for_yields_samples(
    streaming_nyquist: NyquistDevice,
) -> None:
    """``async for sample in stream:`` yields ``Sample`` instances
    until the loop body breaks. We collect a small bounded batch
    so the test stays under the slice's short-engagement budget."""
    stream = streaming_nyquist.create_stream(
        channels=[0], sample_rate=200, async_mode=True
    )
    collected: list[Sample] = []
    stop_exc: Exception | None = None
    try:
        await stream.start()
        async for sample in stream:
            collected.append(sample)
            if len(collected) >= 5:
                break
        assert len(collected) == 5
        assert all(isinstance(s, Sample) for s in collected)
    finally:
        try:
            await stream.stop()
        except Exception as e:
            stop_exc = e

    if stop_exc is not None:
        pytest.fail(f"async stream.stop() failed in cleanup: {stop_exc!r}")


async def test_anext_raises_stop_async_iteration_when_not_running(
    streaming_nyquist: NyquistDevice,
) -> None:
    """Per the contract: ``__anext__`` raises ``StopAsyncIteration``
    when the underlying StreamManager isn't RUNNING. So an
    async-for over a never-started stream completes without
    yielding anything."""
    stream = streaming_nyquist.create_stream(
        channels=[0], sample_rate=100, async_mode=True
    )
    # Never started — state is STOPPED. async-for should exit
    # immediately without producing any samples.
    collected: list[Sample] = []
    async for sample in stream:
        collected.append(sample)
    assert collected == []
