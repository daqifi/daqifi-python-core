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
from typing import Iterator

import pytest

from daqifi.devices import DaqifiDeviceFactory, DaqifiStreamingDevice
from daqifi.discovery import DeviceInfo
from daqifi.enums import WifiMode, WifiSecurityType
from daqifi.network import NetworkConfiguration


def _open_factory_device(port: str) -> DaqifiStreamingDevice:
    """Same factory shape the other B-slice tests use — opens a
    DaqifiStreamingDevice via the canonical factory + initialize
    sequence."""
    return DaqifiDeviceFactory.connect_serial(port.strip())


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
def test_update_network_configuration_round_trip(
    streaming_device: DaqifiStreamingDevice,
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

    saved_ssid = os.environ.get("DAQIFI_TEST_WIFI_SSID")
    saved_password = os.environ.get("DAQIFI_TEST_WIFI_PASSWORD")
    if not saved_ssid or not saved_password:
        pytest.skip(
            "DAQIFI_TEST_WIFI_SSID + DAQIFI_TEST_WIFI_PASSWORD not set; "
            "round-trip would leave the device mis-configured"
        )

    # --- STAGE 1: apply a transient config (different from saved) ---
    transient = NetworkConfiguration(
        mode=WifiMode.EXISTING_NETWORK,
        security_type=WifiSecurityType.WPA_PSK_PHRASE,
        ssid="b12-transient-do-not-associate",
        password="b12-transient-password-x",
    )
    dev.update_network_configuration(transient)

    # Snapshot should now reflect the transient config.
    snap = dev.network_configuration
    assert snap.ssid == transient.ssid
    assert snap.mode == transient.mode
    assert snap.security_type == transient.security_type
    # Password cloned through; clear-text in the snapshot is fine
    # because it's process-local (not on the wire).
    assert snap.password == transient.password

    # --- STAGE 2: restore to the rig's known-good Tesla credentials ---
    restore = NetworkConfiguration(
        mode=WifiMode.EXISTING_NETWORK,
        security_type=WifiSecurityType.WPA_PSK_PHRASE,
        ssid=saved_ssid,
        password=saved_password,
    )
    dev.update_network_configuration(restore)

    snap = dev.network_configuration
    assert snap.ssid == saved_ssid
    assert snap.mode == WifiMode.EXISTING_NETWORK
    assert snap.security_type == WifiSecurityType.WPA_PSK_PHRASE

    # --- STAGE 3: clean reboot so WINC reliably re-associates ---
    # Empirically: rapid back-to-back apply+save+apply+save (the
    # transient → restore thrash above) leaves the WINC in a state
    # where the Tesla credentials are saved to flash but the radio
    # never re-joins the AP — the unit sits at the self-hosted-default
    # ip=192.168.1.1 indefinitely. A SYSTem:REboot forces a clean
    # power-up sequence; the saved Tesla config then drives a normal
    # association on next boot.
    from daqifi._internal import scpi_commands
    dev.send(scpi_commands.reboot_device())
