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
