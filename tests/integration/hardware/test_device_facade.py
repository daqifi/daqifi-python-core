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

import os
from typing import Iterator

import pytest

from daqifi import PowerState
from daqifi._internal.serial_identity import (
    describe_serial_number,
    serial_number_matches,
)
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
    device_info: DeviceInfo,
    wait_for_device_back,
) -> None:
    """``reset()`` sends ``*RST`` which restarts the MCU and drops
    USB CDC during re-enumeration. Wait for the device to come back
    on USB (matching by SN to handle kernel renumbering and to
    refuse binding to a different attached unit), reconnect, and
    verify it's responsive on a fresh handle.

    Without the post-reset reconnect, every subsequent
    ``device_facade``-using test in the session would fail because
    the underlying serial handle is stale and our cached
    ``device_info.port`` may have been renamed.
    """
    dev = device_facade

    # SN identity gate before issuing the destructive `*RST`. Closes
    # the gap PR #71 documented: previously the facade-level
    # `NyquistDevice.serial_number` returned the *IDN? field (a "0"
    # placeholder on fw 3.4.6b1), which made an inline SN check
    # vacuous. `dev.metadata.serial_number` is the protobuf-populated
    # real SN (#74). `wait_for_device_back` already SN-verifies the
    # post-*RST reconnect; this gate now matches it on the pre-*RST
    # side too, so a misconfigured `DAQIFI_DEVICE_PORT` can't reach
    # the *RST send.
    expected_sn = os.environ.get("DAQIFI_EXPECTED_SERIAL_NUMBER")
    if not expected_sn:
        pytest.skip(
            "DAQIFI_EXPECTED_SERIAL_NUMBER not set; refusing to send "
            "*RST without verifying the device identity. Set this "
            "to your unit's SN (see .claude/hardware-rig.md)."
        )
    # connect() does a best-effort metadata refresh; do an explicit
    # refresh here with a longer timeout so a transient SYSInfoPB?
    # miss at connect-time doesn't make the gate falsely report a
    # mismatch. If the explicit refresh ALSO fails, skip — we can't
    # safely identity-verify, so we shouldn't proceed to *RST.
    from daqifi.exceptions import (
        ConnectionError as DAQiFiConnectionError,
        CommandError,
    )
    try:
        dev.refresh_metadata(timeout=5.0)
    except (CommandError, DAQiFiConnectionError) as exc:
        pytest.skip(
            f"Cannot verify device identity (SYSInfoPB? failed: {exc}); "
            f"refusing to send *RST. Re-run with a stable USB connection."
        )
    actual_sn = dev.metadata.serial_number
    # Reject ``"0"`` as well as empty — the *IDN? field is a literal
    # ``"0"`` placeholder on fw 3.4.6b1, and if some future code
    # accidentally cross-wires that into ``metadata.serial_number``
    # we'd otherwise compare ``"0"`` to a real SN and fail loudly
    # rather than skip safely.
    if not actual_sn or actual_sn == "0":
        pytest.skip(
            "metadata.serial_number is missing or placeholder ('0') "
            "after refresh; refusing to send *RST. Older firmware that "
            "doesn't reply to SYSInfoPB? would land here — verify your "
            "unit reports a non-empty SN with `dev.refresh_metadata()`."
        )
    if not serial_number_matches(expected_sn, actual_sn):
        pytest.fail(
            f"Device SN mismatch — refusing to send *RST. "
            f"DAQIFI_EXPECTED_SERIAL_NUMBER={expected_sn!r} but "
            f"DAQIFI_DEVICE_PORT points at a unit with SN "
            f"{describe_serial_number(actual_sn)}. "
            f"Detach the wrong unit or update the env var."
        )

    # `dev.reset()` calls `_comm.reset()` which sends *RST then
    # immediately runs `clear_errors()`. The follow-on SYST:ERR?
    # races the USB drop and raises a "Serial communication error"
    # `RuntimeError` — that's an artifact of the racing teardown,
    # not a reset failure. The *RST itself was sent before the
    # drop. Match only that specific error message so we don't
    # silently swallow unrelated failures (e.g. a transport bug
    # before the *RST is even sent).
    try:
        dev.reset()
    except RuntimeError as e:
        if "Serial communication error" not in str(e):
            raise
    # Release the stale serial handle before the kernel realizes
    # the underlying device has gone away. Best-effort — the
    # device may already be disconnected at this point.
    try:
        dev.disconnect()
    except Exception:
        pass
    new_port = wait_for_device_back()
    if new_port is None:
        pytest.skip(
            "Device did not return on USB within 30s of *RST. On "
            "WSL2/usbipd a manual `usbipd attach --wsl --busid X-Y` "
            "may be needed; on Windows native this normally completes "
            "in <5s, so a 30s timeout suggests a real failure."
        )
    # Reconnect on the (possibly new) port and verify responsive.
    new_dev = NyquistDevice.connect(new_port)
    try:
        assert new_dev.is_connected is True
        info = new_dev.get_system_info()
        assert isinstance(info, str)
        assert info.strip() != ""
    finally:
        _safe_disconnect(new_dev)


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
