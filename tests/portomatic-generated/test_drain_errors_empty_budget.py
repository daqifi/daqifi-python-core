"""Regression for the drain_errors empty-budget escape hatch
surfaced by Qodo PR #62 review #1.

Before this fix, an unresponsive device that returned empty
SYST:ERR? replies would loop the full max_iterations cap (256
by default × ~0.1s send_query delay = ~25s stall). Now drain
exits after `empty_budget` consecutive empty / unparseable
replies — bounding the worst case at ~0.3s while still
draining all real errors when the device is responsive."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from daqifi._internal.comm import SerialInterface


def test_empty_responses_trigger_fail_fast() -> None:
    """3 consecutive empty replies should bail (default empty_budget=3)
    BEFORE the 256-iteration cap kicks in."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(return_value="")
    popped = si.drain_errors()
    # Bailed via empty_budget (3 calls), not max_iterations (256).
    assert si.send_query.call_count == 3
    assert popped == []


def test_empty_budget_parameter_overrides_default() -> None:
    """Empty replies up to empty_budget=5 keep iterating; the 6th
    bails."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(return_value="")
    popped = si.drain_errors(empty_budget=5)
    assert si.send_query.call_count == 5
    assert popped == []


def test_real_errors_reset_empty_budget() -> None:
    """A productive reply between empty ones must reset the
    consecutive-empty counter — otherwise we'd give up too early
    on a noisy-but-functioning device."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(side_effect=[
        "",                              # empty 1
        "",                              # empty 2 (1 from budget)
        '-100,"Bad command"',            # productive — resets counter
        "",                              # empty 1 (counter reset)
        "",                              # empty 2
        '-200,"Execution error"',        # productive — resets again
        '0,"No error"',                  # clean — exit
    ])
    popped = si.drain_errors()
    assert popped == ['-100,"Bad command"', '-200,"Execution error"']
    assert si.send_query.call_count == 7


def test_unparseable_reply_counts_as_empty() -> None:
    """A reply without the SCPI <int>,<msg> shape (no comma) is
    unproductive — count it toward the empty budget so noise/echo
    collisions don't keep us looping."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(return_value="garbage-no-comma")
    popped = si.drain_errors()
    # 3 consecutive unparseable replies hits the budget.
    assert si.send_query.call_count == 3
    assert popped == []


def test_responsive_device_still_drains_all_errors() -> None:
    """Existing successful path: when the device keeps replying with
    well-formed errors and finally 'No error', drain to completion
    regardless of empty_budget."""
    si = SerialInterface(port="/dev/null")
    errors = [f'-{i*100},"err{i}"' for i in range(1, 11)]
    si.send_query = MagicMock(side_effect=errors + ['0,"No error"'])
    popped = si.drain_errors()
    assert len(popped) == 10
    assert popped == errors


def test_max_iterations_still_caps_well_formed_loop() -> None:
    """If the device returns well-formed errors forever (no clean
    + no empties), the max_iterations cap still bounds total
    iterations."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(return_value='-100,"Bad command"')
    popped = si.drain_errors(max_iterations=5)
    assert len(popped) == 5
