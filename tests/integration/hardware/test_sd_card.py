"""B10 — SD-card surface (partial).

Exercises the SD-card stack against real hardware. Splits into
two halves based on whether an SD card is physically inserted in
the slot:

**No-card-needed (always run):**

* ``DaqifiStreamingDevice.enable_sd_card_logging`` /
  ``disable_sd_card_logging`` — fire SCPI module enable/disable
  and update the Python-side ``_is_logging_to_sd`` flag.
* ``DaqifiStreamingDevice.set_sd_logging_file_name`` —
  fire-and-forget SCPI write (no error checking, blocked by #16).
* ``DaqifiStreamingDevice.request_sd_card_file_list`` — emits
  SD:LIST? (SCPI write only; the protobuf consumer eats the
  reply per #16, so we just verify it doesn't raise).
* ``is_logging_to_sd_card`` / ``sd_card_files`` properties —
  pure Python state (flag + tuple-snapshot cache).
* ``NetworkConfigurable.prepare_sd_interface`` /
  ``prepare_lan_interface`` — SPI bus mux ops (no card needed).
* ``SdCardOperations`` Protocol — DaqifiStreamingDevice satisfies
  the structural-typing contract.

**Card-needed (auto-skip when no card detected):**

* ``get_sd_card_files`` — does SD:LIST? + parse, requires real
  card to return a meaningful listing.
* ``start_sd_card_logging`` / ``stop_sd_card_logging`` —
  full canonical Protocol flow; needs a writable card.
* ``download_sd_card_file_to_temp`` — pulls a file off the card.
* ``NyquistDevice.sd_*`` family — requires card.

Whether a card is present is auto-detected on fixture entry
(via a one-time ``sd_list_files`` probe). Card-needing tests
skip with a clear "insert SD card to enable" message when the
slot is empty.

Hardware safety: every test puts the device back into LAN-prep
mode at teardown so SD logging can't accidentally persist into
the next session.
"""
from __future__ import annotations

from typing import Iterator

import pytest

from daqifi.devices import DaqifiDeviceFactory, DaqifiStreamingDevice
from daqifi.discovery import DeviceInfo
from daqifi.sdcard import SdCardFileInfo, SdCardOperations


_USB_CDC_BAUD = 115200


@pytest.fixture(scope="module")
def sd_card_present(device_info: DeviceInfo) -> bool:
    """Probe whether an SD card is in the slot (one-time, module-
    scoped). Connects via the high-level NyquistDevice surface
    (which exposes the simpler ``sd_list_files`` helper) and
    looks for the firmware's "No SD Card Detected" sentinel.

    Returns True when a card IS present; False when the slot is
    empty. Card-needing tests skip on False.
    """
    from daqifi.device import NyquistDevice

    port = device_info.port
    if not port:
        return False
    dev = NyquistDevice.connect(port.strip())
    try:
        dev.sd_enable(True)
        files = dev.sd_list_files()
        # Firmware sentinel for empty slot: returns a single
        # "Error !! No SD Card Detected" pseudo-file plus the
        # standard "**ERROR: -200" error-queue marker.
        joined = " ".join(files).lower()
        return "no sd card" not in joined
    except Exception:
        return False
    finally:
        try:
            dev.disconnect()
        except Exception:
            pass


def _open_factory_device(port: str) -> DaqifiStreamingDevice:
    """Open a streaming device via the canonical factory at the
    USB CDC convention baud (matches the rest of the suite)."""
    return DaqifiDeviceFactory.connect_serial(
        port_name=port.strip(),
        baud_rate=_USB_CDC_BAUD,
    )


@pytest.fixture
def streaming_device(device_info: DeviceInfo) -> Iterator[DaqifiStreamingDevice]:
    """Open a DaqifiStreamingDevice. Teardown returns the SPI bus
    to LAN (so SD logging can't accidentally persist) and
    disconnects."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev = _open_factory_device(port)
    try:
        dev.initialize()
        yield dev
    finally:
        # Force the SPI bus back to LAN to disable any leftover
        # SD module engagement. Best-effort — disconnect is what
        # actually matters for test isolation.
        try:
            dev.prepare_lan_interface()
        except Exception:
            pass
        try:
            dev.disconnect()
        except Exception:
            pass


def _require_sd_card(present: bool) -> None:
    if not present:
        pytest.skip(
            "SD card not detected in slot — insert a card and re-run "
            "to exercise the full SD-card stack"
        )


# ---- Pure-Python state (no card needed) ----------------------------------


def test_is_logging_to_sd_card_initial_state_is_false(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """A freshly-initialized device reports ``is_logging_to_sd_card``
    False — no logging session has been started."""
    assert streaming_device.is_logging_to_sd_card is False


def test_sd_card_files_initial_state_is_empty_tuple(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``sd_card_files`` returns ``()`` (empty tuple) before any
    LIST? call. The defensive immutable shape is verified more
    deeply in test_streaming_lifecycle.py — here we just check the
    initial state."""
    files = streaming_device.sd_card_files
    assert files == ()
    assert isinstance(files, tuple)


# ---- SdCardOperations Protocol satisfaction ------------------------------


def test_streaming_device_satisfies_sd_card_operations_protocol(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``DaqifiStreamingDevice`` structurally implements
    ``SdCardOperations``. ``runtime_checkable`` Protocols can be
    asserted with ``isinstance``; this gates a regression where
    a method gets renamed and silently breaks the contract."""
    assert isinstance(streaming_device, SdCardOperations)


# ---- enable / disable_sd_card_logging — flag round-trip ------------------


def test_enable_sd_card_logging_flips_flag_true(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``enable_sd_card_logging()`` writes the SCPI module-enable
    command and updates the Python-side ``_is_logging_to_sd`` flag
    to True. No card required — this exercises the SCPI fire +
    flag mutation only."""
    dev = streaming_device
    assert dev.is_logging_to_sd_card is False
    dev.enable_sd_card_logging()
    assert dev.is_logging_to_sd_card is True


def test_disable_sd_card_logging_flips_flag_false(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``disable_sd_card_logging()`` flips the Python-side flag
    back to False after enable."""
    dev = streaming_device
    dev.enable_sd_card_logging()
    assert dev.is_logging_to_sd_card is True
    dev.disable_sd_card_logging()
    assert dev.is_logging_to_sd_card is False


# ---- set_sd_logging_file_name + request_sd_card_file_list — SCPI fire ----


def test_set_sd_logging_file_name_does_not_raise(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``set_sd_logging_file_name(...)`` fires the SCPI write.
    Returning without raising means the wire path (transport +
    encode + send) is intact. Firmware-side error-queue check
    blocked on python-core#16."""
    streaming_device.set_sd_logging_file_name("test_b10_filename.csv")


def test_request_sd_card_file_list_does_not_raise(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``request_sd_card_file_list()`` is fire-and-forget — it
    emits SD:LIST? + sleeps the SD interface settle delay. No
    raise means the wire path is intact."""
    streaming_device.request_sd_card_file_list()


# ---- SPI bus mux: prepare_sd_interface / prepare_lan_interface -----------


def test_prepare_sd_interface_does_not_raise(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``prepare_sd_interface()`` does the SPI bus dance —
    disable LAN → settle → enable SD → settle. Independent of
    whether a card is in the slot (purely SPI mux)."""
    streaming_device.prepare_sd_interface()


def test_prepare_lan_interface_does_not_raise(
    streaming_device: DaqifiStreamingDevice,
) -> None:
    """``prepare_lan_interface()`` does the inverse SPI bus mux
    + clears the Python-side ``_is_logging_to_sd`` flag."""
    dev = streaming_device
    dev.enable_sd_card_logging()  # so we can verify the flag clear
    assert dev.is_logging_to_sd_card is True
    dev.prepare_lan_interface()
    # Per the docstring side effect: prepare_lan_interface clears
    # the flag because it disables the SD storage SCPI module.
    assert dev.is_logging_to_sd_card is False


# ---- Card-needed: skip if no card in slot --------------------------------


def test_get_sd_card_files_returns_listing(
    streaming_device: DaqifiStreamingDevice,
    sd_card_present: bool,
) -> None:
    """``get_sd_card_files()`` does SD:LIST? + parses the reply
    into ``SdCardFileInfo`` records. Requires a real card —
    skipped when the slot is empty."""
    _require_sd_card(sd_card_present)
    files = streaming_device.get_sd_card_files()
    assert isinstance(files, tuple)
    for f in files:
        assert isinstance(f, SdCardFileInfo)
        assert f.file_name  # non-empty


def test_start_then_stop_sd_card_logging_round_trips(
    streaming_device: DaqifiStreamingDevice,
    sd_card_present: bool,
) -> None:
    """``start_sd_card_logging(file_name=...)`` runs the canonical
    Protocol flow (prepare_sd_interface → set name → enable). We
    immediately call ``stop_sd_card_logging`` so no real data is
    written. Both calls are no-ops on the Python flag if logging
    didn't actually engage on the device side."""
    _require_sd_card(sd_card_present)
    dev = streaming_device

    test_file = "b10_smoke_logging.csv"
    dev.start_sd_card_logging(file_name=test_file)
    try:
        # If start succeeded, the Python flag follows.
        assert dev.is_logging_to_sd_card is True
    finally:
        dev.stop_sd_card_logging()
    assert dev.is_logging_to_sd_card is False
