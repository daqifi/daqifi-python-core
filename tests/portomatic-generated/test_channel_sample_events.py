"""Tests for ticket #99 group B — per-channel SampleReceived events
+ ActiveSample cache on AnalogInputChannel and DigitalIOChannel.

Mirrors C# ``IChannel.SampleReceived`` / ``ActiveSample`` /
``SetActiveSample``. Surface lives on the SCPI-wrapper classes
(daqifi.channels) for the same cross-class reasons documented in
groups A and C — daqifi.devices.AnalogChannel / DigitalChannel are
metadata-only dataclasses; the sample-handling state lives where
the streaming pipeline can reach it.
"""
from unittest.mock import MagicMock

import pytest

# Import DataSample from the public package root — proves callers
# don't need to reach into _internal (regression for Qodo finding on
# PR #108 / #99 group B).
from daqifi import DataSample
from daqifi.channels import AnalogInputChannel, DigitalIOChannel


def _make_analog():
    return AnalogInputChannel(0, MagicMock(), variant="NQ3")


def _make_digital():
    return DigitalIOChannel(0, MagicMock())


# ---------------------------------------------------------------------------
# active_sample cache (C# IChannel.ActiveSample)
# ---------------------------------------------------------------------------


class TestActiveSampleCache:
    @pytest.mark.parametrize("factory", [_make_analog, _make_digital])
    def test_initially_none(self, factory):
        ch = factory()
        assert ch.active_sample is None

    @pytest.mark.parametrize("factory", [_make_analog, _make_digital])
    def test_set_active_sample_populates_cache(self, factory):
        ch = factory()
        s = DataSample(value=1.5)
        ch.set_active_sample(s)
        assert ch.active_sample is s

    @pytest.mark.parametrize("factory", [_make_analog, _make_digital])
    def test_subsequent_set_replaces_cache(self, factory):
        ch = factory()
        ch.set_active_sample(DataSample(value=1.0))
        s2 = DataSample(value=2.0)
        ch.set_active_sample(s2)
        assert ch.active_sample is s2


# ---------------------------------------------------------------------------
# SampleReceived event subscription (C# add/remove handler)
# ---------------------------------------------------------------------------


class TestSampleReceivedHandlers:
    def test_handler_invoked_on_set_active_sample(self):
        ch = _make_analog()
        received = []
        ch.add_sample_received(received.append)
        s = DataSample(value=3.14)
        ch.set_active_sample(s)
        assert received == [s]

    def test_multiple_handlers_all_invoked(self):
        ch = _make_analog()
        a, b = [], []
        ch.add_sample_received(a.append)
        ch.add_sample_received(b.append)
        s = DataSample(value=2.0)
        ch.set_active_sample(s)
        assert a == [s]
        assert b == [s]

    def test_add_same_handler_twice_only_once(self):
        ch = _make_analog()
        received = []
        ch.add_sample_received(received.append)
        ch.add_sample_received(received.append)  # idempotent
        ch.set_active_sample(DataSample(value=1.0))
        assert len(received) == 1

    def test_remove_handler(self):
        ch = _make_analog()
        received = []
        ch.add_sample_received(received.append)
        ch.remove_sample_received(received.append)
        ch.set_active_sample(DataSample(value=1.0))
        assert received == []

    def test_remove_unregistered_handler_is_noop(self):
        ch = _make_analog()
        ch.remove_sample_received(lambda s: None)  # never registered

    def test_handler_exception_does_not_break_others(self):
        ch = _make_analog()

        def bad_handler(s):
            raise RuntimeError("simulated handler bug")

        good_received = []
        ch.add_sample_received(bad_handler)
        ch.add_sample_received(good_received.append)
        s = DataSample(value=1.0)
        ch.set_active_sample(s)
        assert good_received == [s]
        # Cache must still update even though the bad handler raised.
        assert ch.active_sample is s

    def test_handler_exception_is_logged(self, caplog):
        """Silent swallowing made bugs in user-supplied handlers
        invisible — `set_active_sample` now logs handler failures
        at exception level via logging.getLogger(daqifi.channels)."""
        import logging as _logging
        ch = _make_analog()

        def bad_handler(s):
            raise RuntimeError("simulated handler bug")

        ch.add_sample_received(bad_handler)
        with caplog.at_level(_logging.ERROR, logger="daqifi.channels"):
            ch.set_active_sample(DataSample(value=2.0))

        # Use record.getMessage() and record.exc_info — canonical
        # accessors that work regardless of whether a formatter has
        # rendered the record (rec.message / rec.exc_text are
        # formatter-populated and brittle).
        def _matches(rec) -> bool:
            if "SampleReceived handler" not in rec.getMessage():
                return False
            if rec.exc_info is None:
                return False
            _, exc, _ = rec.exc_info
            return isinstance(exc, RuntimeError) and "simulated handler bug" in str(exc)

        assert any(_matches(r) for r in caplog.records), (
            f"expected handler exception in caplog records: {caplog.records}"
        )

    def test_handler_can_remove_self_from_inside_callback(self):
        """Common pattern: a handler unregisters itself after the
        first sample. The list snapshot in set_active_sample makes
        this safe — the iteration doesn't crash."""
        ch = _make_analog()
        seen = []

        def one_shot(s):
            seen.append(s)
            ch.remove_sample_received(one_shot)

        ch.add_sample_received(one_shot)
        ch.set_active_sample(DataSample(value=1.0))
        ch.set_active_sample(DataSample(value=2.0))
        assert len(seen) == 1  # only the first sample fired the handler


# ---------------------------------------------------------------------------
# Per-channel isolation: handlers on one channel don't fire for another.
# ---------------------------------------------------------------------------


def test_handlers_are_per_channel_not_class_level():
    a = _make_analog()
    b = _make_analog()
    a_received, b_received = [], []
    a.add_sample_received(a_received.append)
    b.add_sample_received(b_received.append)

    s = DataSample(value=1.0)
    a.set_active_sample(s)
    assert a_received == [s]
    assert b_received == []


def test_datasample_re_exported_from_public_package_root():
    """Regression for the Qodo finding on PR #108 — callers must be
    able to import `DataSample` from the public `daqifi` package
    root rather than reaching into `daqifi._internal.comm`. Keeps
    the public API stable across future refactors of `_internal`."""
    import daqifi
    assert hasattr(daqifi, "DataSample"), (
        "daqifi.DataSample must remain in the public surface — see "
        "Qodo /agentic_review pass 1 on PR #108 (#99 group B)"
    )
    assert "DataSample" in daqifi.__all__
    # Identity check: the public re-export IS the same class as the
    # _internal one, not a wrapper.
    from daqifi._internal.comm import DataSample as _internal_DataSample
    assert daqifi.DataSample is _internal_DataSample


def test_analog_and_digital_handlers_are_independent():
    a = _make_analog()
    d = _make_digital()
    a_received, d_received = [], []
    a.add_sample_received(a_received.append)
    d.add_sample_received(d_received.append)

    a_sample = DataSample(value=1.0)
    d_sample = DataSample(value=0.0)
    a.set_active_sample(a_sample)
    d.set_active_sample(d_sample)

    assert a_received == [a_sample]
    assert d_received == [d_sample]
