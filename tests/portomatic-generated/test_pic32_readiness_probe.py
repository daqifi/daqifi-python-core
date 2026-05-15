"""Tests for the post-reconnect application-readiness probe.

Ports the C# behavior added in daqifi-core PR #200 (closes upstream #145):
after the serial transport reopens following a JumpToApplication, optionally
poll a caller-supplied probe until it returns True (or the readiness budget
elapses).

Covers:
- Default options have probe=None and validate() succeeds (legacy semantics).
- Setting probe enables validation of the timeout/retry fields.
- Setting probe with timeout >= jumping_to_application_timeout is rejected.
- _wait_for_application_ready returns immediately when probe returns True.
- _wait_for_application_ready retries when probe returns False.
- _wait_for_application_ready retries when probe raises a non-cancel exception.
- _wait_for_application_ready raises TimeoutError after the budget.
- TimeoutError carries the last probe exception as __cause__.
- Outer cancellation propagates as _CancelledError, not swallowed.
"""
from __future__ import annotations

import threading
import time
from typing import Any, List, Optional

import pytest

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.update_options import FirmwareUpdateServiceOptions
from daqifi.firmware.update_service import (
    DefaultFirmwareUpdateService,
    _Cancellation,
    _CancelledError,
)


def _deadline(
    timeout_seconds: float = 5.0,
    cancel_event: Optional[threading.Event] = None,
) -> _Cancellation:
    return _Cancellation(
        state=FirmwareUpdateState.JUMPING_TO_APP,
        operation="test deadline",
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
        timeout_message_builder=lambda secs: f"timed out after {secs}s",
    )


pytestmark = pytest.mark.portomatic_generated


def _service(opts: FirmwareUpdateServiceOptions) -> DefaultFirmwareUpdateService:
    """Build a service instance with the constructor's required collaborators
    stubbed as None — the readiness wait helper only touches ``_options``,
    ``_logger``, and ``_sleep_with_cancel``, none of which need real
    transports or download services.
    """
    svc = DefaultFirmwareUpdateService.__new__(DefaultFirmwareUpdateService)
    svc._options = opts
    import logging

    svc._logger = logging.getLogger("test_pic32_readiness_probe")
    return svc


class TestOptionsValidation:
    def test_default_options_validate_with_probe_none(self):
        opts = FirmwareUpdateServiceOptions()
        # No probe configured — readiness fields are not validated, so
        # callers who don't use the feature don't need to set them.
        opts.validate()
        assert opts.post_reconnect_readiness_probe is None

    def test_probe_enables_timeout_validation(self):
        opts = FirmwareUpdateServiceOptions()
        opts.post_reconnect_readiness_probe = lambda dev, deadline: True
        opts.post_reconnect_readiness_timeout = -1.0  # invalid
        with pytest.raises(ValueError, match="readiness_timeout"):
            opts.validate()

    def test_probe_enables_retry_delay_validation(self):
        opts = FirmwareUpdateServiceOptions()
        opts.post_reconnect_readiness_probe = lambda dev, deadline: True
        opts.post_reconnect_readiness_retry_delay = 0.0  # invalid
        with pytest.raises(ValueError, match="retry_delay"):
            opts.validate()

    def test_readiness_timeout_must_be_less_than_jumping_to_app_timeout(self):
        opts = FirmwareUpdateServiceOptions()
        opts.post_reconnect_readiness_probe = lambda dev, deadline: True
        opts.post_reconnect_readiness_timeout = opts.jumping_to_application_timeout
        with pytest.raises(ValueError, match="strictly less than"):
            opts.validate()

    def test_readiness_timeout_strictly_below_jumping_to_app_passes(self):
        opts = FirmwareUpdateServiceOptions()
        opts.post_reconnect_readiness_probe = lambda dev, deadline: True
        opts.post_reconnect_readiness_timeout = (
            opts.jumping_to_application_timeout - 0.001
        )
        opts.validate()  # no raise

    def test_readiness_timeout_skipped_when_probe_none(self):
        # Even pathological readiness-timeout values are ignored when the
        # probe is None. Callers who don't opt in shouldn't be forced to
        # zero out the legacy defaults.
        opts = FirmwareUpdateServiceOptions()
        opts.post_reconnect_readiness_timeout = 999.0  # > jumping default
        opts.validate()


class TestWaitForApplicationReady:
    def _opts(self, **overrides: Any) -> FirmwareUpdateServiceOptions:
        opts = FirmwareUpdateServiceOptions()
        # Tighten the readiness budget so tests run fast — keep the
        # outer state timeout well above so we can isolate readiness
        # behavior from JUMPING_TO_APP timing.
        opts.post_reconnect_readiness_timeout = 0.5
        opts.post_reconnect_readiness_retry_delay = 0.01
        opts.jumping_to_application_timeout = 5.0
        for k, v in overrides.items():
            setattr(opts, k, v)
        return opts

    def test_returns_immediately_when_probe_true(self):
        opts = self._opts()
        svc = _service(opts)
        deadline = _deadline()

        calls: List[int] = []

        def probe(dev, dl) -> bool:
            calls.append(1)
            return True

        svc._wait_for_application_ready("device-stub", probe, deadline)
        assert calls == [1]  # exactly one probe invocation

    def test_retries_when_probe_returns_false_then_true(self):
        opts = self._opts()
        svc = _service(opts)
        deadline = _deadline()

        attempts = {"n": 0}

        def probe(dev, dl) -> bool:
            attempts["n"] += 1
            return attempts["n"] >= 3

        svc._wait_for_application_ready("dev", probe, deadline)
        assert attempts["n"] == 3

    def test_retries_when_probe_raises_then_succeeds(self):
        opts = self._opts()
        svc = _service(opts)
        deadline = _deadline()

        attempts = {"n": 0}

        def probe(dev, dl) -> bool:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError(f"transient probe error #{attempts['n']}")
            return True

        svc._wait_for_application_ready("dev", probe, deadline)
        assert attempts["n"] == 3

    def test_raises_timeout_when_probe_never_succeeds(self):
        opts = self._opts(post_reconnect_readiness_timeout=0.1)
        svc = _service(opts)
        deadline = _deadline()

        def probe(dev, dl) -> bool:
            return False

        with pytest.raises(TimeoutError, match="application-ready"):
            svc._wait_for_application_ready("dev", probe, deadline)

    def test_timeout_carries_last_probe_exception_as_cause(self):
        opts = self._opts(post_reconnect_readiness_timeout=0.05)
        svc = _service(opts)
        deadline = _deadline()

        # Probe deterministically throws — the eventual TimeoutError
        # should carry the last raised exception as __cause__ so callers
        # see WHY the device never became ready.
        def probe(dev, dl) -> bool:
            raise RuntimeError("transport open but device silent")

        with pytest.raises(TimeoutError) as excinfo:
            svc._wait_for_application_ready("dev", probe, deadline)
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "transport open but device silent" in str(excinfo.value.__cause__)

    def test_timeout_clears_cause_when_last_probe_succeeded_returning_false(self):
        # If the last probe completed normally (returning False), the
        # TimeoutError must NOT carry a stale exception from an earlier
        # attempt as __cause__. Mirrors upstream PR #200's
        # ``lastProbeException = null`` after a successful invocation.
        opts = self._opts(post_reconnect_readiness_timeout=0.2)
        svc = _service(opts)
        deadline = _deadline()

        attempts = {"n": 0}

        def probe(dev, dl) -> bool:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("first attempt fails")
            return False  # subsequent attempts complete normally

        with pytest.raises(TimeoutError) as excinfo:
            svc._wait_for_application_ready("dev", probe, deadline)
        # The first-attempt exception should NOT be the cause —
        # subsequent successful (False) attempts cleared it.
        assert excinfo.value.__cause__ is None

    def test_outer_cancellation_propagates_as_cancelled_error(self):
        # _CancelledError from the outer deadline must NOT be swallowed
        # as "probe failure, retry" — it indicates the user / outer state
        # timeout asked us to stop, and we honor that immediately.
        opts = self._opts()
        svc = _service(opts)
        cancel = threading.Event()
        cancel.set()  # cooperative cancel pre-armed
        deadline = _deadline(cancel_event=cancel)

        def probe(dev, dl) -> bool:
            raise _CancelledError()

        with pytest.raises(_CancelledError):
            svc._wait_for_application_ready("dev", probe, deadline)
