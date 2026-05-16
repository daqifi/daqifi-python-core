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
from daqifi.streaming import Sample, StreamManager


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
