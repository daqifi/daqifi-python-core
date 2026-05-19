"""Tests for ticket #107 — wire per-channel SampleReceived dispatch
from the streaming pipeline.

Closes the loop that #99 group B opened: the ``_SampleReceivedMixin``
surface on AnalogInputChannel / DigitalIOChannel was already there,
but until now the StreamManager produced ``Sample`` objects and only
fired the user callback — per-channel handlers stayed silent unless
the caller wired dispatch by hand. This module verifies that
``NyquistDevice.create_stream`` now installs a dispatch callback that
fans out every ``Sample.channels`` entry to its corresponding
channel's ``set_active_sample`` (which in turn fires the
``SampleReceived`` handler chain).
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from daqifi import DataSample
from daqifi.channels import AnalogInputChannel, AnalogInputs
from daqifi.streaming import Sample, StreamManager, StreamState


# ---------------------------------------------------------------------------
# StreamManager dispatcher itself (unit-level, no device wrapper)
# ---------------------------------------------------------------------------


class TestStreamManagerPerChannelDispatch:
    def test_dispatch_called_per_channel_value(self):
        """All channels in a single Sample receive the same anchored
        epoch timestamp; values fan out per channel index."""
        import time

        comm = MagicMock()
        calls = []

        def dispatcher(idx, value, ts):
            calls.append((idx, value, ts))

        sm = StreamManager(comm, [0, 1, 2], per_channel_dispatch=dispatcher)
        before = time.time()
        sample = Sample(timestamp=1234.5, channels={0: 1.0, 1: 2.0, 2: 3.0})
        sm._dispatch_per_channel(sample)
        after = time.time()

        # First sample anchors: epoch ≈ wall-clock, regardless of the
        # device-relative Sample.timestamp value.
        assert [(c[0], c[1]) for c in calls] == [(0, 1.0), (1, 2.0), (2, 3.0)]
        for _, _, ts in calls:
            assert before <= ts <= after
        # All three channels in the same Sample share one timestamp.
        assert len({c[2] for c in calls}) == 1

    def test_relative_timestamps_anchor_to_wall_clock(self):
        """Subsequent Samples maintain monotonic wall-clock spacing
        equal to the spacing of their relative device timestamps —
        verifies the Qodo PR 142 pass 2 timestamp-basis fix."""
        import time

        comm = MagicMock()
        calls = []

        def dispatcher(idx, value, ts):
            calls.append(ts)

        sm = StreamManager(comm, [0], per_channel_dispatch=dispatcher)
        before = time.time()
        # Three samples, 0.5s apart in device-relative time.
        sm._dispatch_per_channel(Sample(timestamp=10.0, channels={0: 1.0}))
        sm._dispatch_per_channel(Sample(timestamp=10.5, channels={0: 2.0}))
        sm._dispatch_per_channel(Sample(timestamp=11.0, channels={0: 3.0}))

        # Anchored at wall-clock; subsequent gaps preserved exactly.
        assert calls[0] >= before
        assert abs((calls[1] - calls[0]) - 0.5) < 1e-6
        assert abs((calls[2] - calls[1]) - 0.5) < 1e-6

    def test_dispatch_no_op_when_unset(self):
        comm = MagicMock()
        sm = StreamManager(comm, [0, 1])
        sample = Sample(timestamp=0.0, channels={0: 1.0, 1: 2.0})
        # Should not raise / should not require any external state.
        sm._dispatch_per_channel(sample)

    def test_re_anchors_on_backwards_jump(self):
        """A backwards device-time jump (counter wrap or mid-stream
        reset) must re-anchor to current wall-clock so emitted epoch
        values stay sane. (Qodo PR #142 pass 3 #2.)"""
        import time

        comm = MagicMock()
        calls = []
        sm = StreamManager(
            comm, [0], per_channel_dispatch=lambda i, v, t: calls.append(t)
        )

        sm._dispatch_per_channel(Sample(timestamp=100.0, channels={0: 1.0}))
        time.sleep(0.01)
        # Now jump device-time backwards by 50s.
        before = time.time()
        sm._dispatch_per_channel(Sample(timestamp=50.0, channels={0: 2.0}))
        after = time.time()

        # Second emission must be near current wall-clock, not in the past.
        assert before <= calls[1] <= after
        # And it must NOT be 50s before the first (the naive path
        # would yield calls[0] - 50 which is ~50s in the past).
        assert calls[1] >= calls[0]

    def test_re_anchors_on_unreasonable_forward_jump(self):
        """A per-sample step of >60s triggers re-anchor; protects
        against tick counter glitches and device resets. The threshold
        is per-sample step, not cumulative elapsed time (Qodo PR #142
        pass 5 #4)."""
        import time

        comm = MagicMock()
        calls = []
        sm = StreamManager(
            comm, [0], per_channel_dispatch=lambda i, v, t: calls.append(t)
        )

        sm._dispatch_per_channel(Sample(timestamp=10.0, channels={0: 1.0}))
        # 5min jump forward in ONE step — beyond the 60s per-sample
        # threshold.
        before = time.time()
        sm._dispatch_per_channel(Sample(timestamp=310.0, channels={0: 2.0}))
        after = time.time()

        # Re-anchored — second emission ≈ current wall-clock, not
        # 5 minutes after first.
        assert before <= calls[1] <= after
        assert calls[1] - calls[0] < 5.0  # would be 300 without re-anchor

    def test_no_spurious_reanchor_on_long_healthy_stream(self):
        """A stream that runs longer than 60 device-relative seconds
        with healthy per-sample steps must NOT re-anchor — the guard
        is per-sample, not cumulative. (Qodo PR #142 pass 5 #4: the
        prior cumulative-delta version forced a re-anchor every
        ~60s on a healthy stream, introducing jitter in the dispatched
        epoch_seconds.)"""
        import time

        comm = MagicMock()
        calls = []
        sm = StreamManager(
            comm, [0], per_channel_dispatch=lambda i, v, t: calls.append(t)
        )

        # First sample anchors.
        sm._dispatch_per_channel(Sample(timestamp=0.0, channels={0: 1.0}))
        # 700 samples, 0.1s apart → 70s of relative time, all healthy.
        for i in range(1, 701):
            sm._dispatch_per_channel(
                Sample(timestamp=i * 0.1, channels={0: float(i)})
            )

        # The last call's epoch should be ~70s after the first call's
        # epoch — proving the anchor was kept and only delta accumulated.
        assert abs((calls[-1] - calls[0]) - 70.0) < 1e-3, (
            f"expected ~70.0s gap, got {calls[-1] - calls[0]}"
        )

    def test_drops_non_finite_timestamps_without_poisoning_anchor(self):
        """NaN / inf in Sample.timestamp must be dropped silently and
        must NOT poison _dispatch_last_sample_ts. NaN comparisons all
        evaluate False, so without an isfinite check the re-anchor
        guard never fires and every subsequent dispatch produces NaN
        epoch_seconds. (Qodo PR #142 pass 6 importance 9.)"""
        import math

        comm = MagicMock()
        calls = []
        sm = StreamManager(
            comm, [0], per_channel_dispatch=lambda i, v, t: calls.append(t)
        )

        # Prime with a healthy sample.
        sm._dispatch_per_channel(Sample(timestamp=10.0, channels={0: 1.0}))
        good_ts = sm._dispatch_last_sample_ts

        # NaN must not be remembered as the new anchor.
        sm._dispatch_per_channel(
            Sample(timestamp=float("nan"), channels={0: 2.0})
        )
        assert sm._dispatch_last_sample_ts == good_ts
        assert len(calls) == 1  # NaN sample skipped entirely

        # +inf same treatment.
        sm._dispatch_per_channel(
            Sample(timestamp=float("inf"), channels={0: 3.0})
        )
        assert sm._dispatch_last_sample_ts == good_ts
        assert len(calls) == 1

        # Pipeline still works after the bad samples.
        sm._dispatch_per_channel(Sample(timestamp=10.5, channels={0: 4.0}))
        assert len(calls) == 2
        assert math.isfinite(calls[1])

    def test_anchor_resets_on_stop_start_reuse(self):
        """Reusing a StreamManager across sessions (start → stop →
        start) must reset the dispatch anchor. Without the reset, the
        second session's first sample would compute epoch_seconds as
        (prev_session_epoch_base + (new_relative - prev_relative))
        instead of anchoring fresh against current wall-clock.
        (Qodo PR #142 pass 4 inline finding.)"""
        import time

        comm = MagicMock()
        sm = StreamManager(
            comm, [0], per_channel_dispatch=lambda i, v, t: None
        )

        # Session 1: prime the anchor.
        sm._dispatch_per_channel(Sample(timestamp=100.0, channels={0: 1.0}))
        assert sm._dispatch_sample_base == 100.0
        assert sm._dispatch_epoch_base is not None

        # Simulate stop → start by calling start (which resets state).
        # Stub _comm.send_query because start does a SCPI probe.
        comm.send_query = MagicMock(return_value="1")
        # Avoid actually launching the streaming thread / sending SCPI.
        sm._stop_event.set()
        # We only care that the *reset path* runs — wrap try/except.
        try:
            sm.start()
        except Exception:
            pass

        assert sm._dispatch_sample_base is None, (
            "anchor must reset on start() so reused StreamManager "
            "doesn't drag a stale epoch base into the next session"
        )
        assert sm._dispatch_epoch_base is None

    def test_dispatch_fires_from_blocking_read(self):
        """`stream.read()` (polling/blocking mode) must also fan out to
        per-channel handlers — not just callback mode. Otherwise users
        who poll with `.read()` get silent SampleReceived handlers.
        (Qodo PR #142 pass 3 #1.)"""
        comm = MagicMock()
        calls = []
        sm = StreamManager(
            comm, [0], per_channel_dispatch=lambda i, v, t: calls.append((i, v))
        )
        sm._state = StreamState.RUNNING
        # Stub _read_sample to return one sample then None forever.
        produced = [Sample(timestamp=1.0, channels={0: 7.0})]

        def fake_read():
            return produced.pop(0) if produced else None

        sm._read_sample = fake_read
        result = sm.read(num_samples=1, timeout=1.0)

        assert len(result) == 1
        assert calls == [(0, 7.0)]

    def test_misbehaving_handler_is_swallowed_per_channel(self):
        """A handler that raises must not poison the streaming thread
        nor starve later channels in the same Sample."""
        comm = MagicMock()
        seen = []

        def dispatcher(idx, value, ts):
            seen.append(idx)
            if idx == 1:
                raise RuntimeError("boom")

        sm = StreamManager(comm, [0, 1, 2], per_channel_dispatch=dispatcher)
        sample = Sample(timestamp=0.0, channels={0: 1.0, 1: 2.0, 2: 3.0})
        sm._dispatch_per_channel(sample)
        # All three were attempted even though channel 1 raised.
        assert seen == [0, 1, 2]


# ---------------------------------------------------------------------------
# End-to-end: device-built dispatcher routes into AnalogInputChannel
# ---------------------------------------------------------------------------


def _make_analog_inputs(n=4):
    """Build a small AnalogInputs collection with real (non-mock)
    AnalogInputChannel instances so the SampleReceived mixin behaves
    normally."""
    shared_range = [None]
    chs = [
        AnalogInputChannel(i, MagicMock(), variant="NQ3", range_cache=shared_range)
        for i in range(n)
    ]
    return AnalogInputs(chs)


class TestDeviceWiring:
    def test_dispatch_sets_active_sample_on_addressed_channel(self):
        """NyquistDevice._build_per_channel_dispatch must update the
        right channel's active_sample."""
        from daqifi.device import NyquistDevice

        dev = NyquistDevice.__new__(NyquistDevice)  # bypass __init__ I/O
        dev._analog_inputs = _make_analog_inputs(n=4)
        dispatch = dev._build_per_channel_dispatch()

        dispatch(2, 1.234, 1700000000.0)

        assert dev._analog_inputs[2].active_sample is not None
        assert dev._analog_inputs[2].active_sample.value == 1.234
        # Other channels untouched.
        for i in (0, 1, 3):
            assert dev._analog_inputs[i].active_sample is None

    def test_dispatch_fires_sample_received_handler(self):
        """The mixin's add_sample_received chain must be invoked
        when dispatch routes a value to the channel."""
        from daqifi.device import NyquistDevice

        dev = NyquistDevice.__new__(NyquistDevice)
        dev._analog_inputs = _make_analog_inputs(n=2)
        dispatch = dev._build_per_channel_dispatch()

        received = []
        dev._analog_inputs[1].add_sample_received(received.append)
        dispatch(1, 5.5, 1700000000.0)

        assert len(received) == 1
        assert isinstance(received[0], DataSample)
        assert received[0].value == 5.5
        assert received[0].timestamp == datetime.fromtimestamp(
            1700000000.0, tz=timezone.utc
        )

    def test_dispatch_skips_out_of_range_index(self):
        """Streaming-thread dispatch must not raise on a stray channel
        index — the streaming loop has to keep running."""
        from daqifi.device import NyquistDevice

        dev = NyquistDevice.__new__(NyquistDevice)
        dev._analog_inputs = _make_analog_inputs(n=2)
        dispatch = dev._build_per_channel_dispatch()

        dispatch(99, 1.0, 0.0)  # Should not raise.
        for i in range(2):
            assert dev._analog_inputs[i].active_sample is None

    def test_dispatch_tolerates_malformed_timestamp(self):
        """A garbage timestamp (out-of-range float from a decode glitch)
        must not poison the streaming thread. (Qodo PR 142 pass 1
        importance 5.)"""
        from daqifi.device import NyquistDevice

        dev = NyquistDevice.__new__(NyquistDevice)
        dev._analog_inputs = _make_analog_inputs(n=2)
        dispatch = dev._build_per_channel_dispatch()

        # Out-of-range float — datetime.fromtimestamp raises OverflowError
        dispatch(0, 1.0, 1e20)
        # Channel was skipped (no sample reached the cache).
        assert dev._analog_inputs[0].active_sample is None

    def test_dispatch_no_op_when_inputs_uninitialized(self):
        """Pre-connect (or non-NQ3) state where _analog_inputs is None
        must not crash dispatch."""
        from daqifi.device import NyquistDevice

        dev = NyquistDevice.__new__(NyquistDevice)
        dev._analog_inputs = None
        dispatch = dev._build_per_channel_dispatch()

        dispatch(0, 1.0, 0.0)  # Should not raise.

    def test_full_sample_fans_out_to_all_subscribed_channels(self):
        """Subscribers on multiple channels each see only their own
        DataSample when a multi-channel Sample is dispatched."""
        from daqifi.device import NyquistDevice

        dev = NyquistDevice.__new__(NyquistDevice)
        dev._analog_inputs = _make_analog_inputs(n=4)
        dispatch = dev._build_per_channel_dispatch()

        # Subscribe to channels 0 and 2.
        seen_0, seen_2 = [], []
        dev._analog_inputs[0].add_sample_received(seen_0.append)
        dev._analog_inputs[2].add_sample_received(seen_2.append)

        sample = Sample(
            timestamp=1700000000.5,
            channels={0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0},
        )
        # Drive through StreamManager's dispatcher with the device's
        # closure to exercise the full wiring.
        comm = MagicMock()
        sm = StreamManager(comm, [0, 1, 2, 3], per_channel_dispatch=dispatch)
        sm._dispatch_per_channel(sample)

        assert [s.value for s in seen_0] == [1.0]
        assert [s.value for s in seen_2] == [3.0]
        # Channels 1 and 3 had values but no subscriber — still must
        # have set the active_sample cache.
        assert dev._analog_inputs[1].active_sample.value == 2.0
        assert dev._analog_inputs[3].active_sample.value == 4.0
