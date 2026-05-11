"""Tests for ticket #99 group A — AnalogChannel range bounds.

Mirrors C# ``IAnalogChannel.MinValue`` / ``MaxValue``. NQ1/NQ2 are
fixed ±5V (MC12bADC / AD7173). NQ3 is derived from the configured
range setting on the AD7609 module (±5V or ±10V).
"""
from unittest.mock import MagicMock

import pytest

from daqifi._internal import scpi as _scpi
from daqifi.channels import AnalogInputChannel


def _make_input(variant, range_response="0"):
    comm = MagicMock()
    # send_query returns the configured range. Default "0" → ±5V on NQ3.
    comm.send_query = MagicMock(return_value=range_response)
    return AnalogInputChannel(channel_id=0, comm=comm, variant=variant)


class TestNQ1NQ2FixedRange:
    @pytest.mark.parametrize("variant", ["NQ1", "NQ2"])
    def test_min_value_is_negative_five_volts(self, variant):
        ch = _make_input(variant)
        assert ch.min_value == -5.0

    @pytest.mark.parametrize("variant", ["NQ1", "NQ2"])
    def test_max_value_is_positive_five_volts(self, variant):
        ch = _make_input(variant)
        assert ch.max_value == 5.0

    @pytest.mark.parametrize("variant", ["NQ1", "NQ2"])
    def test_no_scpi_query_for_fixed_range_variants(self, variant):
        """The ±5V bounds are hardcoded for NQ1/NQ2 — no SCPI roundtrip."""
        ch = _make_input(variant)
        ch._comm.send_query.reset_mock()
        _ = ch.min_value
        _ = ch.max_value
        assert ch._comm.send_query.call_count == 0


class TestNQ3DerivedRange:
    def test_range_zero_yields_five_volts(self):
        ch = _make_input("NQ3", range_response="0")
        assert ch.min_value == -5.0
        assert ch.max_value == 5.0

    def test_range_one_yields_ten_volts(self):
        ch = _make_input("NQ3", range_response="1")
        assert ch.min_value == -10.0
        assert ch.max_value == 10.0

    def test_query_failure_falls_back_to_ad7609_default(self):
        """If the SCPI range query fails, fall back to ±10V (AD7609
        default per firmware NQ3RuntimeDefaults.c)."""
        comm = MagicMock()
        comm.send_query = MagicMock(side_effect=Exception("device timeout"))
        ch = AnalogInputChannel(channel_id=0, comm=comm, variant="NQ3")
        assert ch.min_value == -10.0
        assert ch.max_value == 10.0

    def test_unknown_range_response_falls_back_to_default(self):
        ch = _make_input("NQ3", range_response="weird")
        assert ch.min_value == -10.0
        assert ch.max_value == 10.0


class TestRangeBoundsCaching:
    """The ticket explicitly required a cached lookup. First access
    pays the SCPI cost; subsequent accesses are O(1). Invalidation
    happens on the range setter — caught by Qodo /agentic_review pass
    1 on PR #106."""

    def test_first_access_queries_device(self):
        ch = _make_input("NQ3", range_response="0")
        ch._comm.send_query.reset_mock()
        _ = ch.min_value
        assert ch._comm.send_query.call_count == 1

    def test_second_access_uses_cache(self):
        ch = _make_input("NQ3", range_response="0")
        _ = ch.min_value  # populates cache
        ch._comm.send_query.reset_mock()
        _ = ch.min_value
        _ = ch.max_value
        assert ch._comm.send_query.call_count == 0

    def test_min_max_consistent_snapshot(self):
        """If the device range changes between min_value and max_value
        accesses, both reads must reflect the same snapshot — the
        cache guarantees this. Without the cache, min_value would see
        range '0' and max_value range '1' and produce a (-5, 10)
        nonsense bound."""
        ch = _make_input("NQ3", range_response="0")
        first_min = ch.min_value
        # Simulate device range change between accesses.
        ch._comm.send_query = MagicMock(return_value="1")
        first_max = ch.max_value
        assert (first_min, first_max) == (-5.0, 5.0), (
            f"cache should pin the snapshot to (-5, 5), got "
            f"({first_min}, {first_max})"
        )

    def test_query_failure_does_not_cache_fallback(self):
        """If the SCPI query fails, fall back to ±10V but DON'T cache
        the fallback — the next access should retry the query so the
        channel can recover from a transient timeout."""
        comm = MagicMock()
        comm.send_query = MagicMock(side_effect=Exception("transient timeout"))
        ch = AnalogInputChannel(channel_id=0, comm=comm, variant="NQ3")
        assert ch.min_value == -10.0  # fallback

        # Now the device recovers and replies "0" → ±5V.
        comm.send_query = MagicMock(return_value="0")
        ch._comm = comm  # ensure new mock is in place
        # Properly re-attach
        assert ch._comm.send_query.call_count == 0
        assert ch.min_value == -5.0  # query happens again, returns ±5V
        assert ch._comm.send_query.call_count == 1

    def test_range_setter_invalidates_cache(self):
        """After calling the range setter, the next min_value /
        max_value access must re-query the device — the previous
        bounds are stale."""
        ch = _make_input("NQ3", range_response="0")
        comm = ch._comm
        comm.send_set_with_error = MagicMock(return_value=(True, '0,"No error"'))

        assert ch.min_value == -5.0  # populates cache
        comm.send_query.reset_mock()

        # Set range to "1" (±10V). The setter sends a write, then
        # the cache is invalidated.
        ch.range = "1"
        # Update query response so the re-read returns the new range.
        comm.send_query = MagicMock(return_value="1")
        assert ch.min_value == -10.0
        assert ch.max_value == 10.0

    def test_nq1_nq2_does_not_use_cache(self):
        """Hardcoded variants don't need a cache layer (no SCPI on
        the path), and the cache holder stays [None]."""
        ch = _make_input("NQ1")
        _ = ch.min_value
        _ = ch.max_value
        assert ch._range_cache == [None]


class TestSharedRangeCacheAcrossChannels:
    """Range is module-level on NQ3 (AD7609 register), so a range
    write on any single channel invalidates the bounds for ALL of
    its siblings on the same module. Bug caught by Qodo
    /agentic_review pass 2 on PR #106 — pre-fix, each channel had
    its own cache and a write on channel 0 left channels 1-7 with
    stale (-5,5) values for the rest of the session."""

    def _make_module(self, variant="NQ3", initial_range="0", num_channels=4):
        comm = MagicMock()
        comm.send_query = MagicMock(return_value=initial_range)
        comm.send_set_with_error = MagicMock(
            return_value=(True, '0,"No error"')
        )
        shared = [None]
        return [
            AnalogInputChannel(
                i, comm, variant, range_cache=shared
            )
            for i in range(num_channels)
        ], comm, shared

    def test_first_access_on_any_channel_populates_shared_cache(self):
        channels, comm, shared = self._make_module()
        comm.send_query.reset_mock()
        _ = channels[0].min_value
        # Cache populated by channel 0; channels 1-3 see it via the
        # shared list and don't issue further queries.
        for c in channels[1:]:
            _ = c.min_value
            _ = c.max_value
        assert comm.send_query.call_count == 1
        assert shared[0] == (-5.0, 5.0)

    def test_range_setter_on_one_channel_invalidates_all(self):
        channels, comm, shared = self._make_module()
        # Populate shared cache with ±5V.
        _ = channels[0].min_value
        _ = channels[3].min_value
        assert shared[0] == (-5.0, 5.0)

        # Write range "1" on channel 2 → cache invalidated.
        channels[2].range = "1"
        assert shared[0] is None

        # Now any sibling's next access re-queries and gets the new
        # bounds.
        comm.send_query = MagicMock(return_value="1")
        assert channels[0].min_value == -10.0
        assert channels[5 % len(channels)].max_value == 10.0

    def test_solo_construction_gets_private_cache(self):
        """Channels constructed without a shared cache list each get
        their own — solo-construction callers (tests, ad-hoc usage)
        keep the original isolated behavior."""
        comm = MagicMock()
        comm.send_query = MagicMock(return_value="0")
        a = AnalogInputChannel(0, comm, "NQ3")
        b = AnalogInputChannel(1, comm, "NQ3")
        # Each has its own list — populate one, the other remains
        # empty until accessed.
        _ = a.min_value
        assert a._range_cache[0] == (-5.0, 5.0)
        assert b._range_cache[0] is None
        assert a._range_cache is not b._range_cache


class TestNoVariantSpecified:
    """When variant is None (caller didn't pass it), the channel
    behaves like NQ3 — query the configured range. Matches the
    existing `range` property's behavior."""

    def test_default_range_zero(self):
        comm = MagicMock()
        comm.send_query = MagicMock(return_value="0")
        ch = AnalogInputChannel(channel_id=0, comm=comm, variant=None)
        assert ch.min_value == -5.0
        assert ch.max_value == 5.0

    def test_default_range_one(self):
        comm = MagicMock()
        comm.send_query = MagicMock(return_value="1")
        ch = AnalogInputChannel(channel_id=0, comm=comm, variant=None)
        assert ch.min_value == -10.0
        assert ch.max_value == 10.0
