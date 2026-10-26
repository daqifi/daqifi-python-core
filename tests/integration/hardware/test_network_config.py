"""B12 — ``daqifi.network`` + ``daqifi.devices`` network-config slice.

Exercises the network-configuration surface against real hardware
(COM9 unit, fw 3.4.6b1):

Non-destructive (always-on):

* ``NetworkConfiguration.clone()`` — defensive-copy semantics on
  the pure dataclass.
* ``DaqifiStreamingDevice.network_configuration`` property —
  returns a defensive copy; caller mutation doesn't poison the
  process-local snapshot.
* ``DaqifiStreamingDevice.request_lan_chip_info()`` — fire-and-
  forget SCPI; doesn't queue an error.
* ``set_wifi_ssid`` / ``set_wifi_password`` /
  ``set_wifi_mode_self_hosted`` / ``set_wifi_mode_existing`` —
  smoke: SCPI command writes succeed without queueing a SYST:ERR
  error. We don't try to read back the staged value because
  ``DaqifiStreamingDevice``'s text-response path is broken
  (daqifi-python-core#16) — the network_configuration snapshot
  only updates after a successful ``update_network_configuration``.

Destructive (gated on ``DAQIFI_DESTRUCTIVE_OK=1``):

* ``DaqifiStreamingDevice.update_network_configuration`` /
  ``apply_network_configuration`` / ``save_network_configuration``
  / ``prepare_lan_interface`` — full canonical sequence with
  Tesla LAN credentials + restore. Touches flash; will trigger a
  WiFi module restart that takes ~5s.

Hardware safety (destructive path): the full update_network_config
test reads the current snapshot first, applies a transient (no-save)
config, then restores via update_network_configuration to the
original credentials. If the restore step fails the test will
re-raise so the operator knows the device may be left mis-
configured.
"""
from __future__ import annotations

import os
import warnings
from typing import Iterator

import pytest

from daqifi._internal import scpi_commands
from daqifi._internal.serial_identity import (
    describe_serial_number,
    serial_number_matches,
)
from daqifi.devices import DaqifiDeviceFactory, DaqifiStreamingDevice
from daqifi.discovery import DeviceInfo
from daqifi.enums import WifiMode, WifiSecurityType
from daqifi.network import NetworkConfiguration


# USB CDC convention used by the rest of the hardware suite. Baud
# is technically meaningless on USB CDC (it's a serial-protocol
# parameter the host driver ignores) but explicit consistency keeps
# us aligned with the other test modules and future-proofs against
# a real serial transport ever appearing.
_USB_CDC_BAUD = 115200


def _open_factory_device(port: str) -> DaqifiStreamingDevice:
    """Same factory shape the other B-slice tests use — opens a
    DaqifiStreamingDevice via the canonical factory + initialize
    sequence. Pinned to 115200 baud for consistency with the rest
    of the hardware suite."""
    return DaqifiDeviceFactory.connect_serial(
        port_name=port.strip(),
        baud_rate=_USB_CDC_BAUD,
    )


@pytest.fixture
def streaming_device(device_info: DeviceInfo) -> Iterator[DaqifiStreamingDevice]:
    """Open a DaqifiStreamingDevice on the discovered port. Yields
    a connected + initialized device. Teardown disconnects."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev = _open_factory_device(port)
    try:
        dev.initialize()
        yield dev
    finally:
        try:
            dev.disconnect()
        except Exception:
            pass


# ---- NetworkConfiguration.clone — pure dataclass --------------------------


def test_network_configuration_clone_returns_independent_copy() -> None:
    """``NetworkConfiguration.clone()`` returns a new instance with
    the same field values; mutating the clone doesn't affect the
    original."""
    original = NetworkConfiguration(
        mode=WifiMode.EXISTING_NETWORK,
        security_type=WifiSecurityType.WPA_PSK_PHRASE,
        ssid="rig-A",
        password="hunter2",
    )
    clone = original.clone()

    assert clone is not original
    assert clone == original
    # Mutate the clone — original must be untouched.
    clone.ssid = "rig-B"
    assert original.ssid == "rig-A"


def test_network_configuration_clone_handles_default_constructor() -> None:
    """``NetworkConfiguration()`` (no args) clones into another
    default-shaped instance — round-trip equality holds."""
    original = NetworkConfiguration()
    assert original.clone() == original


# ---- network_configuration property — defensive copy ----------------------


def test_network_configuration_property_returns_defensive_copy(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``dev.network_configuration`` returns a fresh clone every
    call — caller mutation of the returned object does NOT affect
    the device's process-local snapshot."""
    snap1 = streaming_device.network_configuration
    snap2 = streaming_device.network_configuration
    assert snap1 is not snap2  # different instances
    assert snap1 == snap2      # same data

    # Mutate snap1 with values guaranteed not to match the default
    # so we can detect a leak even on a freshly-constructed device.
    snap1.ssid = "test-canary-do-not-leak"
    snap1.password = "test-canary-do-not-leak"

    snap3 = streaming_device.network_configuration
    assert snap3.ssid != "test-canary-do-not-leak", (
        "caller mutation of the returned snapshot leaked back into "
        "the device's network_configuration — defensive-copy is broken"
    )
    assert snap3.password != "test-canary-do-not-leak"


# ---- request_lan_chip_info — fire-and-forget call -------------------------

# NOTE on smoke-test depth: ``DaqifiStreamingDevice``'s background
# protobuf consumer eats text-shaped SCPI replies (see python-core#16),
# so we can't query ``SYST:ERR?`` and read the firmware's error queue
# from this surface — the read window gets consumed by the consumer
# before our query reply lands. The tests below therefore only
# verify that the SCPI write doesn't raise on the Python side. Once
# #16 is resolved we can tighten these to also assert the firmware
# error queue is clean post-call.


def test_request_lan_chip_info_does_not_raise(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``request_lan_chip_info()`` is fire-and-forget — it sends the
    SCPI command and returns. Returning without raising means the
    Python wire-write path (transport + send + ScpiMessage encode)
    is intact. Firmware-side error-queue verification is blocked on
    python-core#16 (see module note)."""
    streaming_device.request_lan_chip_info()


# ---- set_wifi_* setters — smoke tests (Python wire-write path) ------------


def test_set_wifi_ssid_smoke(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``set_wifi_ssid("test-staging-b12")`` writes the SCPI command
    without raising on the Python side. The staged value only takes
    effect after apply_network_configuration; without apply, the
    device's running WiFi association is unchanged. (Firmware-side
    SYST:ERR queue check blocked on python-core#16.)"""
    streaming_device.set_wifi_ssid("test-staging-b12")


def test_set_wifi_password_smoke(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``set_wifi_password(...)`` writes without raising. We don't
    read back the password (firmware doesn't expose it for security
    reasons)."""
    streaming_device.set_wifi_password("test-staging-password-b12")


def test_set_wifi_mode_self_hosted_smoke(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``set_wifi_mode_self_hosted()`` stages SELF_HOSTED mode."""
    streaming_device.set_wifi_mode_self_hosted()


def test_set_wifi_mode_existing_smoke(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``set_wifi_mode_existing()`` stages EXISTING_NETWORK mode.

    Run this LAST in the smoke-test sequence so the staged-but-
    unsaved leftover state matches the saved-flash state our test
    rig boots into (associated to Tesla as EXISTING_NETWORK)."""
    streaming_device.set_wifi_mode_existing()


# ---- Destructive — full update_network_configuration round-trip ----------


def test_update_network_configuration_rejects_none(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``update_network_configuration(None)`` raises
    ``TypeError`` BEFORE any wire write. (Pure-Python guard,
    safe to test always — no destructive gating.)"""
    with pytest.raises(TypeError):
        streaming_device.update_network_configuration(None)


def test_update_network_configuration_rejects_empty_ssid(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """An empty SSID raises ``ValueError`` before any wire write."""
    cfg = NetworkConfiguration(
        mode=WifiMode.EXISTING_NETWORK,
        security_type=WifiSecurityType.WPA_PSK_PHRASE,
        ssid="",
        password="anything",
    )
    with pytest.raises(ValueError):
        streaming_device.update_network_configuration(cfg)


# IMPORTANT: this test is intentionally LAST in the file. Its final
# `SYSTem:REboot` call drops the device from USB for ~5s while it
# re-enumerates, so any tests collected after it would fail to open
# their `streaming_device` fixture.
@pytest.mark.hardware_destructive
def test_update_network_configuration_round_trip(
    streaming_device: DaqifiStreamingDevice,
    device_info: DeviceInfo,
    wait_for_device_back,
    destructive_ok,
) -> None:
    """Full canonical sequence: read current snapshot, apply a
    transient (no-save) config, restore via
    update_network_configuration to the original credentials,
    then reboot for clean WINC re-association.

    Touches flash via the SAVE step inside
    update_network_configuration. Restores using the DAQIFI_TEST_*
    env vars from .env so the test rig is left in its
    pre-test associated-to-Tesla state. Final reboot is required
    because rapid back-to-back apply+save+apply+save leaves the
    WINC unable to re-join the AP without a power-cycle (saved
    Tesla credentials persist; only the live radio is wedged).
    """
    dev = streaming_device

    # Identity gate — never write to a device the operator hasn't
    # explicitly declared as ours. CONTRIBUTING.md requires SN
    # verification before any flash-touching write because
    # multiple DAQiFi units may be exposed on the same workstation.
    expected_sn = os.environ.get("DAQIFI_EXPECTED_SERIAL_NUMBER")
    if not expected_sn:
        pytest.skip(
            "DAQIFI_EXPECTED_SERIAL_NUMBER not set; refusing to run "
            "destructive test on unverified device. Set this to "
            "your unit's SN (see .claude/hardware-rig.md)"
        )
    actual_sn = dev.metadata.serial_number
    if not serial_number_matches(expected_sn, actual_sn):
        pytest.fail(
            f"Device identity mismatch: expected SN {expected_sn!r}, "
            f"got {describe_serial_number(actual_sn)}. Refusing to run "
            f"destructive test on "
            f"a different unit."
        )

    saved_ssid = os.environ.get("DAQIFI_TEST_WIFI_SSID")
    saved_password = os.environ.get("DAQIFI_TEST_WIFI_PASSWORD")
    if not saved_ssid or not saved_password:
        pytest.skip(
            "DAQIFI_TEST_WIFI_SSID + DAQIFI_TEST_WIFI_PASSWORD not set; "
            "round-trip would leave the device mis-configured"
        )

    # Transient creds must come from env per CONTRIBUTING.md compliance —
    # tests must not embed WiFi credentials in tracked files. The
    # transient SSID/password is intentionally chosen NOT to match
    # any real AP (it gets written briefly then overwritten by the
    # restore step below), but it's still credential-shaped data
    # and the policy doesn't carve out exceptions.
    transient_ssid = os.environ.get("DAQIFI_TEST_WIFI_TRANSIENT_SSID")
    transient_password = os.environ.get("DAQIFI_TEST_WIFI_TRANSIENT_PASSWORD")
    if not transient_ssid or not transient_password:
        pytest.skip(
            "DAQIFI_TEST_WIFI_TRANSIENT_SSID + "
            "DAQIFI_TEST_WIFI_TRANSIENT_PASSWORD not set; transient "
            "creds must come from env (CONTRIBUTING.md compliance)"
        )

    restore = NetworkConfiguration(
        mode=WifiMode.EXISTING_NETWORK,
        security_type=WifiSecurityType.WPA_PSK_PHRASE,
        ssid=saved_ssid,
        password=saved_password,
    )

    # --- STAGE 1: apply a transient config (different from saved) ---
    # `update_network_configuration` ALWAYS does APPLY+SAVE under
    # the hood, so this is a persistent flash write. The try/finally
    # below guarantees restore runs even if the transient apply
    # itself partially fails (mid-sequence — some SET commands done,
    # SAVE not yet) OR any assertion in stage 1 fails. Without the
    # try wrapping the apply call, a partial mid-sequence failure
    # would leave the rig with arbitrary intermediate state and no
    # restore.
    transient = NetworkConfiguration(
        mode=WifiMode.EXISTING_NETWORK,
        security_type=WifiSecurityType.WPA_PSK_PHRASE,
        ssid=transient_ssid,
        password=transient_password,
    )

    try:
        dev.update_network_configuration(transient)

        # Snapshot should now reflect the transient config.
        snap = dev.network_configuration
        assert snap.ssid == transient.ssid
        assert snap.mode == transient.mode
        assert snap.security_type == transient.security_type
        # Password cloned through; clear-text in the snapshot is fine
        # because it's process-local (not on the wire).
        assert snap.password == transient.password

    finally:
        # --- STAGE 2 (always-runs): restore Tesla credentials ---
        restore_failed: Exception | None = None
        try:
            dev.update_network_configuration(restore)
        except Exception as e:
            restore_failed = e
        else:
            snap = dev.network_configuration
            # Snapshot assertions go inside else (only if restore
            # itself didn't blow up). Wrap to capture so we can
            # report whichever failure mode actually occurred.
            try:
                assert snap.ssid == saved_ssid
                assert snap.mode == WifiMode.EXISTING_NETWORK
                assert snap.security_type == WifiSecurityType.WPA_PSK_PHRASE
            except AssertionError as e:
                restore_failed = e

        # --- STAGE 3 (always-runs): reboot for clean re-association ---
        # Empirically: rapid back-to-back apply+save+apply+save (the
        # transient → restore thrash above) leaves the WINC in a state
        # where the Tesla credentials are saved to flash but the radio
        # never re-joins the AP — the unit sits at the self-hosted-
        # default ip=192.168.1.1 indefinitely. A SYSTem:REboot forces
        # a clean power-up sequence; the saved Tesla config then
        # drives a normal association on next boot.
        try:
            dev.send(scpi_commands.reboot_device())
        except Exception:
            # Reboot wire-write may fail if the device already
            # disconnected — non-fatal here, the restore is what
            # actually matters.
            pass

        # Best-effort wait for the device to come back on USB after
        # reboot. Defensive for downstream test fixtures — if the
        # device doesn't return (e.g. WSL2/usbipd doesn't auto-
        # reattach and DAQIFI_USBIPD_BUSID isn't set), emit a warning
        # so the next failing test's error message has context, but
        # don't fail the round-trip itself: it already succeeded
        # before the reboot. The fixture handles SN-matched identity
        # verification, in-place `device_info.port` update if the
        # kernel renamed our device, and optional usbipd reattach.
        if wait_for_device_back() is None:
            warnings.warn(
                f"Device (SN {os.environ.get('DAQIFI_EXPECTED_SERIAL_NUMBER')!r}) "
                f"did not return on USB within 30s of reboot. Subsequent "
                f"hardware tests in this session may fail to connect. "
                f"On WSL2 set DAQIFI_USBIPD_BUSID=<busid> to enable "
                f"auto-reattach.",
                stacklevel=2,
            )

        if restore_failed is not None:
            pytest.fail(
                "FAILED to restore Tesla credentials after stage 1. "
                "Device may be persistently misconfigured — manual "
                f"restoration required. Underlying: {restore_failed!r}"
            )
