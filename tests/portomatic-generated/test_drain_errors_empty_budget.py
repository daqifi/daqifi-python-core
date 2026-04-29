"""Regression for the drain_errors empty-budget escape hatch
surfaced by Qodo PR #62 review #1.

Before this fix, an unresponsive device that returned empty
SYST:ERR? replies would loop the full max_iterations cap (256
by default × ~0.1s send_query delay = ~25s stall). Now drain
exits after `empty_budget` consecutive empty / unparseable
replies — bounding the worst case at ~0.3s while still
draining all real errors when the device is responsive.

When drain exits via empty_budget OR max_iterations without
seeing the canonical 0,"No error" reply, a synthetic
DRAIN_INCONCLUSIVE sentinel is appended so callers can detect
"queue not confirmed clean" — mirrors the conftest.py pattern
(Qodo PR #64 review finding 1)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from daqifi._internal.comm import SerialInterface


def _has_inconclusive(popped: list[str]) -> bool:
    return any(p.startswith("DRAIN_INCONCLUSIVE,") for p in popped)


def test_empty_responses_trigger_fail_fast() -> None:
    """3 consecutive empty replies should bail (default empty_budget=3)
    BEFORE the 256-iteration cap kicks in."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(return_value="")
    popped = si.drain_errors()
    # Bailed via empty_budget (3 calls), not max_iterations (256).
    assert si.send_query.call_count == 3
    # Inconclusive sentinel — no "No error" was ever observed.
    assert len(popped) == 1
    assert _has_inconclusive(popped)


def test_empty_budget_parameter_overrides_default() -> None:
    """Empty replies up to empty_budget=5 keep iterating; the 6th
    bails."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(return_value="")
    popped = si.drain_errors(empty_budget=5)
    assert si.send_query.call_count == 5
    assert _has_inconclusive(popped)


def test_real_errors_reset_empty_budget() -> None:
    """A productive reply between empty ones must reset the
    consecutive-empty counter — otherwise we'd give up too early
    on a noisy-but-functioning device. Final 'No error' confirms
    the queue is drained, so NO inconclusive sentinel is appended."""
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
    assert not _has_inconclusive(popped)
    assert si.send_query.call_count == 7


def test_unparseable_reply_counts_as_empty() -> None:
    """A reply without the SCPI <int>,<msg> shape (no comma) is
    unproductive — count it toward the empty budget so noise/echo
    collisions don't keep us looping. Inconclusive on exit."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(return_value="garbage-no-comma")
    popped = si.drain_errors()
    # 3 consecutive unparseable replies hits the budget.
    assert si.send_query.call_count == 3
    assert _has_inconclusive(popped)


def test_non_int_code_counts_as_empty() -> None:
    """A reply with a comma but where the code field isn't an int
    is also unproductive (likely echo collision). Strict shape per
    Qodo PR #64 review finding 3."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(return_value='not_an_int,"some text"')
    popped = si.drain_errors()
    assert si.send_query.call_count == 3
    assert _has_inconclusive(popped)


def test_no_error_in_stream_does_not_terminate_early() -> None:
    """A real error payload like 120,\"No error in stream\" must NOT
    be treated as the canonical clean reply — it has code != 0.
    Loose substring matching for 'No error' would terminate early
    here and falsely report a clean queue (Qodo PR #64 finding 3)."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(side_effect=[
        '120,"No error in stream"',  # real error — code != 0
        '0,"No error"',              # actual clean reply
    ])
    popped = si.drain_errors()
    assert popped == ['120,"No error in stream"']
    assert not _has_inconclusive(popped)


def test_responsive_device_still_drains_all_errors() -> None:
    """Existing successful path: when the device keeps replying with
    well-formed errors and finally 'No error', drain to completion
    with NO inconclusive sentinel."""
    si = SerialInterface(port="/dev/null")
    errors = [f'-{i*100},"err{i}"' for i in range(1, 11)]
    si.send_query = MagicMock(side_effect=errors + ['0,"No error"'])
    popped = si.drain_errors()
    assert popped == errors
    assert not _has_inconclusive(popped)


def test_max_iterations_caps_and_marks_inconclusive() -> None:
    """If the device returns well-formed errors forever (no 'No error'),
    the max_iterations cap bounds iterations AND appends the
    inconclusive sentinel."""
    si = SerialInterface(port="/dev/null")
    si.send_query = MagicMock(return_value='-100,"Bad command"')
    popped = si.drain_errors(max_iterations=5)
    # 5 real errors plus the inconclusive sentinel.
    assert len(popped) == 6
    assert popped[-1].startswith("DRAIN_INCONCLUSIVE,")
    assert popped[:-1] == ['-100,"Bad command"'] * 5
