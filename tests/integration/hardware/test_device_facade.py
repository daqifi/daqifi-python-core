"""B9 — ``daqifi.device.NyquistDevice`` high-level facade slice.

Exercises every public symbol on the user-facing facade against
real hardware (COM9 unit, fw 3.4.6b1):

* ``NyquistDevice.connect`` — accepts both a ``str`` port path and
  a ``DeviceInfo`` payload from discovery.
* ``NyquistDevice.disconnect`` — flips ``is_connected`` False; safe
  to call twice.
* ``NyquistDevice.is_connected`` — tracks connect/disconnect.
* ``NyquistDevice.variant`` / ``manufacturer`` / ``model`` /
  ``serial_number`` / ``firmware_version`` — populated from the
  ``*IDN?`` response captured during connect.
* ``NyquistDevice.power_state`` — get/set round-trip;
  ``power_up`` / ``power_down`` are convenience wrappers.
* ``NyquistDevice.reset`` — sends ``*RST`` and brings the device
  back to a known-clean baseline.
* ``NyquistDevice.get_system_info`` — non-empty string from
  ``SYST:INFO?``.
* ``NyquistDevice.get_last_error`` — returns "0,\\"No error\\""
  on a clean error queue.
* ``NyquistDevice.clear_errors`` — drains the queue (capped at 10
  per python-core#22).
* ``NyquistDevice.__enter__`` / ``__exit__`` — context manager
  disconnects on exit, even when the body raises.

Hardware safety: every test ends with ``power_down()`` and
``disconnect()`` so a later session inherits a quiet device.
"""
from __future__ import annotations

from typing import Iterator

import pytest

from daqifi import PowerState
from daqifi.device import NyquistDevice
from daqifi.discovery import BoardVariant, DeviceInfo


# ---- Local fixture ---------------------------------------------------------


@pytest.fixture
def device_facade(device_info: DeviceInfo) -> Iterator[NyquistDevice]:
    """Open a ``NyquistDevice`` on the discovered port and yield it
    powered up + with errors cleared. Teardown powers down +
    disconnects unconditionally."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev = NyquistDevice.connect(port.strip())
    try:
        dev.clear_errors()
        dev.power_up()
        yield dev
    finally:
        try:
            dev.power_down()
        except Exception:
            # Power-down is best-effort in teardown — we still need
            # to disconnect even if the device isn't responding.
            pass
        try:
            dev.disconnect()
        except Exception:
            pass


# ---- connect — both signatures ---------------------------------------------


def _safe_disconnect(dev: NyquistDevice) -> None:
    """Best-effort disconnect for tests that manage their own
    NyquistDevice (i.e. don't go through the `device_facade`
    fixture). A teardown-time disconnect failure shouldn't cascade
    into the pytest output and bury the actual test outcome."""
    try:
        dev.disconnect()
    except Exception:
        pass


def test_connect_accepts_string_port(device_info: DeviceInfo) -> None:
    """``NyquistDevice.connect("/dev/ttyACMx")`` opens, identifies,
    and populates the variant from the IDN reply."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev = NyquistDevice.connect(port.strip())
    try:
        assert dev.is_connected is True
        # Variant must be one of the known board values — UNKNOWN
        # would mean the IDN parser couldn't recognize the device.
        assert dev.variant in (
            BoardVariant.NQ1,
            BoardVariant.NQ2,
            BoardVariant.NQ3,
        )
    finally:
        _safe_disconnect(dev)


def test_connect_accepts_device_info(device_info: DeviceInfo) -> None:
    """``NyquistDevice.connect(device_info)`` extracts the port from
    the discovery payload — same end-state as the string form."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    # device_info from the session fixture may have whitespace in port;
    # build a clean copy so the connect path doesn't normalize-then-
    # fail on a trimmed-port mismatch.
    cleaned = DeviceInfo(
        port=port.strip(),
        variant=device_info.variant,
        manufacturer=device_info.manufacturer,
        model=device_info.model,
        serial_number=device_info.serial_number,
        firmware_version=device_info.firmware_version,
    )
    dev = NyquistDevice.connect(cleaned)
    try:
        assert dev.is_connected is True
    finally:
        _safe_disconnect(dev)


# ---- disconnect / is_connected --------------------------------------------


def test_is_connected_tracks_disconnect(device_info: DeviceInfo) -> None:
    """``is_connected`` flips True after ``connect``, False after
    ``disconnect``. ``disconnect`` is idempotent — a second call
    after the first must not raise."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev = NyquistDevice.connect(port.strip())
    try:
        assert dev.is_connected is True
        dev.disconnect()
        assert dev.is_connected is False
        # Second disconnect: no raise.
        dev.disconnect()
        assert dev.is_connected is False
    finally:
        _safe_disconnect(dev)


# ---- IDN-derived metadata --------------------------------------------------


def test_idn_metadata_is_populated(device_facade: NyquistDevice) -> None:
    """``manufacturer`` / ``model`` / ``serial_number`` /
    ``firmware_version`` are populated from the IDN reply at
    connect-time. Non-empty strings on a real device."""
    dev = device_facade
    assert isinstance(dev.manufacturer, str) and dev.manufacturer
    assert isinstance(dev.model, str) and dev.model
    assert isinstance(dev.serial_number, str) and dev.serial_number
    assert isinstance(dev.firmware_version, str) and dev.firmware_version


# ---- power_state get/set + power_up/power_down -----------------------------


def test_power_up_brings_device_to_powered_up(
    device_facade: NyquistDevice,
) -> None:
    """``power_up()`` writes ``PowerState.POWERED_UP`` and the
    subsequent ``power_state`` read confirms the new state."""
    dev = device_facade
    dev.power_up()
    assert dev.power_state == PowerState.POWERED_UP


def test_power_down_returns_device_to_standby(
    device_facade: NyquistDevice,
) -> None:
    """``power_down()`` writes ``PowerState.STANDBY``; the read-back
    confirms. (Powered up by the fixture, so this is a real
    transition, not a no-op.)"""
    dev = device_facade
    dev.power_down()
    assert dev.power_state == PowerState.STANDBY
    # Return to powered-up so the fixture's clean teardown doesn't
    # have to skip the post-test power_down.
    dev.power_up()


# ---- reset -----------------------------------------------------------------


def test_reset_returns_to_clean_baseline(
    device_facade: NyquistDevice,
) -> None:
    """``reset()`` sends ``*RST``; the device should still be
    connected (USB CDC stays up) and still respond to subsequent
    queries."""
    dev = device_facade
    dev.reset()
    assert dev.is_connected is True
    # Round-trip a benign query to confirm the device is still
    # responsive after the reset.
    info = dev.get_system_info()
    assert isinstance(info, str)


# ---- get_system_info -------------------------------------------------------


def test_get_system_info_returns_non_empty_string(
    device_facade: NyquistDevice,
) -> None:
    """``get_system_info()`` issues ``SYST:INFO?`` and returns a
    non-empty string. We don't assert the format — different
    firmware versions emit different fields — just that the device
    responded."""
    dev = device_facade
    info = dev.get_system_info()
    assert isinstance(info, str)
    assert info.strip() != ""


# ---- get_last_error / clear_errors -----------------------------------------


def test_get_last_error_returns_no_error_on_clean_queue(
    device_facade: NyquistDevice,
) -> None:
    """After ``clear_errors()``, ``get_last_error()`` returns the
    canonical no-error reply. We match loosely (``"No error" in
    reply``) because firmware quote/whitespace handling varies."""
    dev = device_facade
    dev.clear_errors()
    err = dev.get_last_error()
    assert isinstance(err, str)
    assert "No error" in err, (
        f"expected canonical no-error reply, got {err!r}"
    )


def test_clear_errors_drains_queue(device_facade: NyquistDevice) -> None:
    """``clear_errors()`` drains the queue. After clearing, the
    next ``get_last_error()`` returns the canonical no-error
    reply — proves the drain took effect (even if no errors were
    pending to begin with)."""
    dev = device_facade
    dev.clear_errors()
    err = dev.get_last_error()
    assert "No error" in err


# ---- Context manager -------------------------------------------------------


def test_context_manager_disconnects_on_normal_exit(
    device_info: DeviceInfo,
) -> None:
    """``with NyquistDevice.connect(...) as dev:`` disconnects on
    block exit — ``is_connected`` is False after the block.

    Uses the canonical inline pattern (`with NyquistDevice.connect(...)
    as dev:`) so the test exercises the same shape users would
    actually write."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev: NyquistDevice
    with NyquistDevice.connect(port.strip()) as dev:
        assert dev.is_connected is True
    assert dev.is_connected is False


def test_context_manager_disconnects_on_exception(
    device_info: DeviceInfo,
) -> None:
    """The context manager disconnects even when the with-body
    raises — the exception propagates AND the device disconnects.
    Critical for robust test cleanup."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    sentinel_message = "deliberate-test-exception-b9-context-manager"
    captured: dict[str, NyquistDevice] = {}
    with pytest.raises(RuntimeError, match=sentinel_message):
        with NyquistDevice.connect(port.strip()) as dev:
            captured["dev"] = dev
            assert dev.is_connected is True
            raise RuntimeError(sentinel_message)
    # The with-block exited via exception; verify __exit__ still
    # disconnected the device the user got from `as dev`.
    assert captured["dev"].is_connected is False
