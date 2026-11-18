"""pc#280: the "no bootloader" check starts watching too late.

A board without the PIC32 bootloader treats FORceBoot as a plain reboot: its
serial port drops and comes back as the application. The service has a precise
refusal for that, but it only ever looked for the port's absence AFTER the
1.5 s settle delay -- by which time the absence can be over -- so the user got
a generic 45 s timeout instead.

Two changes are covered here:
  1. the target port is sampled from the FORceBoot write THROUGH the settle,
     so the absence is recorded when it happens;
  2. the cause's own text reaches ``str(exc)``, and the no-bootloader case
     gets its own recovery guidance, selected by exception TYPE.

TIMING: this codebase has no injectable clock and no existing test patches
``time``, so these tests use the real clock with deliberately wide margins --
the absence window is many sample intervals long, and nothing depends on a
single sample landing. The port fake RECORDS every call with a timestamp and
the tests print it on failure, so a timing flake is diagnosable rather than
mysterious.

⛔ WHY THE "ABSENCE ENDS AFTER THE SETTLE" TIMELINE IS NOT ASSERTED HERE.
On real hardware the absence outlasted the settle by only ~0.08 s, against an
effective poll cadence of ~0.28 s -- so the OLD code detects it roughly 29% of
the time. A test asserting a base outcome for that shape would be flaky by
construction. The timeline asserted instead has the absence ENTIRELY inside
the settle, which the old code cannot see and the new code must: deterministic
in both directions, and it is the case the sampling cadence exists for.
"""

import importlib.util
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

from daqifi._internal.comm import HidDeviceInfo
from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.bootloader import Pic32BootloaderProtocol
from daqifi.firmware.update_events import FirmwareUpdateException
from daqifi.firmware.update_service import DefaultFirmwareUpdateService

_spec = importlib.util.spec_from_file_location(
    "service_fakes_280",
    Path(__file__).parent / "portomatic-generated" / "test_default_firmware_update_service.py",
)
_f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_f)

TARGET_PORT = "COM255"     # ⛔ synthetic and allowlisted. NEVER a real
                           # bench port: this repo is public, and
                           # tests/test_public_tree_hygiene.py exists to
                           # keep workstation detail out of it.
OTHER_PORTS = frozenset({"COM1", "COM3"})
SETTLE = 0.30          # many sample intervals; see TIMING above


@pytest.fixture(autouse=True)
def _no_application_ports_on_the_host(monkeypatch):
    import daqifi.firmware.update_service as update_service

    monkeypatch.setattr(update_service, "_list_usb_serial_ports", lambda vid, pid: [])


class _PortTimeline:
    """A ``_list_serial_port_names`` fake whose window is anchored to the WRITE.

    ⛔ AN EARLIER VERSION ANCHORED t0 TO ITS OWN FIRST CALL, AND THAT MADE THE
    TEST GREEN FOR THE WRONG REASON. At the base the first call is the wait
    loop's first look, so an absence "starting" then was always visible and
    the guard fired at the base too -- the tests then failed at the base only
    on the message and guidance assertions, leaving change 1 UNTESTED. The
    window must start when FORceBoot is WRITTEN, exactly as on hardware, so
    code that starts watching after the settle genuinely misses it.

    ``mark_write()`` is called from the fake device's ``send``.
    """

    def __init__(self, absent_from, absent_until):
        self.absent_from = absent_from
        self.absent_until = absent_until
        self.t_write = None
        self.calls = []            # (elapsed_since_write_or_None, present)

    def mark_write(self):
        if self.t_write is None:
            self.t_write = time.monotonic()

    def __call__(self):
        if self.t_write is None:
            # Before the write there is nothing to be absent for.
            self.calls.append((None, True))
            return OTHER_PORTS | {TARGET_PORT}
        elapsed = time.monotonic() - self.t_write
        absent = self.absent_from <= elapsed < self.absent_until
        self.calls.append((round(elapsed, 4), not absent))
        return OTHER_PORTS if absent else OTHER_PORTS | {TARGET_PORT}

    def saw_absent(self):
        return any(present is False for _, present in self.calls)

    def report(self):
        return ("calls=%d absent_seen=%d first=%r last=%r"
                % (len(self.calls),
                   sum(1 for _, p in self.calls if p is False),
                   self.calls[:3], self.calls[-3:]))


# ---- a valid lower-panel image ------------------------------------------
# ⛔ NOT _f._make_temp_hex(): that writes a placeholder for the WiFi path and
# the PIC32 parser rejects it ("doesn't start with the colon character").
# Same shape as test_pic32_flash_safety.py's _GOOD.

def _line(record: bytes) -> str:
    return ":" + (record + bytes([(-sum(record)) & 0xFF])).hex().upper()


def _image_lines():
    base = _line(bytes([2, 0, 0, 4, 0x1D, 0x00]))
    reset = _line(bytes([8, 0x00, 0x00, 0]) + bytes(range(8)))
    bulk = _line(bytes([16, 0x04, 0x80, 0]) + bytes(range(0x40, 0x50)))
    return [base, reset, bulk, ":00000001FF"]


def _write_image() -> str:
    handle, path = tempfile.mkstemp(suffix=".hex")
    with os.fdopen(handle, "w") as fh:
        fh.write("\n".join(_image_lines()) + "\n")
    return path


def _never_absent():
    def _listing():
        return OTHER_PORTS | {TARGET_PORT}
    return _listing


def _options(settle=SETTLE):
    options = _f._fast_options()
    # ⛔ validate() requires >= 40 s even for a PIC32 update, which never
    # touches the WiFi bridge. _fast_options() sets 0.2, so every construction
    # raises without this -- the same line test_pic32_flash_safety.py carries.
    options.wifi_recovery_timeout = 40.0
    # ⛔ Assigned AFTER construction on purpose when settle == 0:
    # _validate_positive rejects post_force_boot_delay <= 0, so a zero settle
    # is NOT reachable through the public options API, while the comment at
    # update_service.py:579 still says "which validate() allows". Mutation is
    # not re-validated, which is the only way to exercise the zero path -- and
    # that path is exactly where _sleep_with_cancel IS the cancel check.
    options.post_force_boot_delay = settle
    # Long enough that the wait loop really does time out rather than racing
    # the test, short enough to keep the suite quick.
    options.waiting_for_bootloader_timeout = 1.0
    return options


def _service(enumerator, options=None):
    return DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=_f._FakeExternalProcessRunner(),
        bootloader_protocol=Pic32BootloaderProtocol(),
        hid_device_enumerator=enumerator,
        options=options or _options(),
    )


def _no_bootloader_ever():
    return _f._FakeHidEnumerator([[]])


def _bootloader_appears():
    return _f._FakeHidEnumerator(
        [[], [HidDeviceInfo(0x04D8, 0x003C, "path-1", "", "Bootloader")]]
    )


def _run(monkeypatch, listing, enumerator, options=None, cancel_event=None,
         post_construct=None):
    import daqifi.firmware.update_service as update_service

    monkeypatch.setattr(update_service, "_list_serial_port_names", listing)
    monkeypatch.setattr(update_service, "serial_port_location", lambda port: None)
    monkeypatch.setattr(update_service, "hid_device_location", lambda path: None)
    device = _f._FakeStreamingDevice()
    device.transport = type("T", (), {"port_name": TARGET_PORT})()
    # Anchor the timeline to the FORceBoot write, not to the first listing.
    if hasattr(listing, "mark_write"):
        _original_send = device.send

        def _send(message, _orig=_original_send, _t=listing):
            _t.mark_write()
            return _orig(message)

        device.send = _send
    service = _service(enumerator, options)
    if post_construct is not None:
        post_construct(service)
    path = _write_image()
    try:
        service.update_firmware(device, path, cancel_event=cancel_event)
        return None
    except FirmwareUpdateException as exc:
        return exc
    finally:
        os.remove(path)


def _names_the_bootloader(exc):
    return "does not appear to carry the PIC32 bootloader" in str(exc)


# ---- change 1: the absence is recorded while it happens -----------------


def test_an_absence_entirely_inside_the_settle_is_caught(monkeypatch):
    """BASE: generic timeout, the guard never fires. FIX: precise refusal.

    The port is absent from 0.05 s to 0.12 s after the first look, well
    inside the 0.30 s settle, so code that only starts watching afterwards
    cannot see it.
    """
    timeline = _PortTimeline(0.05, 0.12)
    exc = _run(monkeypatch, timeline, _no_bootloader_ever())
    assert exc is not None, "the update should not succeed with no bootloader"
    assert exc.failed_state == FirmwareUpdateState.WAITING_FOR_BOOTLOADER, (
        "the refusal must still be reported against the waiting state; %s"
        % timeline.report()
    )
    assert _names_the_bootloader(exc), (
        "str(exc) should name the missing bootloader, got %r (%s)"
        % (str(exc), timeline.report())
    )
    assert timeline.saw_absent(), (
        "the fake was never shown absent, so this asserts nothing: %s"
        % timeline.report()
    )


def test_a_port_that_never_goes_absent_still_times_out(monkeypatch):
    """Unchanged behaviour: no absence, so no refusal -- a plain timeout."""
    exc = _run(monkeypatch, _never_absent(), _no_bootloader_ever())
    assert exc is not None
    assert exc.failed_state == FirmwareUpdateState.WAITING_FOR_BOOTLOADER
    assert not _names_the_bootloader(exc), (
        "nothing was ever absent, so the no-bootloader refusal must NOT fire: %r"
        % str(exc)
    )


def test_a_bootloader_that_appears_is_not_refused(monkeypatch):
    """The fix must not false-fire: port goes absent and a bootloader shows up."""
    timeline = _PortTimeline(0.05, 10.0)      # absent and stays absent
    exc = _run(monkeypatch, timeline, _bootloader_appears())
    assert exc is None or not _names_the_bootloader(exc), (
        "a real bootloader appeared; the no-bootloader refusal must not fire: %r (%s)"
        % (exc and str(exc), timeline.report())
    )


# ---- change 2: the message and the guidance -----------------------------


def test_the_message_carries_the_causes_own_text(monkeypatch):
    """str(exc) must carry the cause, for the refusal AND the plain timeout."""
    exc = _run(monkeypatch, _never_absent(), _no_bootloader_ever())
    assert exc is not None
    assert str(exc).startswith("Firmware update failed in state"), (
        "the first sentence must be unchanged so existing matchers keep working"
    )
    assert "timed out" in str(exc) or "No matching HID bootloader" in str(exc), (
        "the timeout's own detail should reach str(exc), got %r" % str(exc)
    )


def test_the_no_bootloader_case_gets_its_own_guidance(monkeypatch):
    timeline = _PortTimeline(0.05, 0.12)
    exc = _run(monkeypatch, timeline, _no_bootloader_ever())
    assert exc is not None and _names_the_bootloader(exc), timeline.report()
    guidance = exc.recovery_guidance or ""
    assert "bootloader" in guidance and "programmer" in guidance, (
        "the no-bootloader case should be told to program a bootloader, got %r"
        % guidance
    )
    assert "replug" not in guidance.lower(), (
        "replugging cannot add a bootloader, so that advice is wrong here: %r"
        % guidance
    )


def test_a_plain_timeout_keeps_the_existing_guidance(monkeypatch):
    exc = _run(monkeypatch, _never_absent(), _no_bootloader_ever())
    assert exc is not None
    guidance = (exc.recovery_guidance or "").lower()
    assert "replug" in guidance or "unplug" in guidance, (
        "an ordinary timeout must keep its existing guidance, got %r"
        % exc.recovery_guidance
    )


def test_the_guidance_is_keyed_on_type_not_wording():
    """The selection must not depend on the refusal's wording.

    Wording is what a later edit is free to change; matching on it would be
    in-band signalling.
    """
    import daqifi.firmware.update_service as update_service

    dedicated = getattr(
        update_service, "_TargetReturnedToApplicationError", None
    )
    assert dedicated is not None, "a dedicated exception type should exist"
    assert issubclass(dedicated, RuntimeError), (
        "it must stay a RuntimeError so existing handlers are unaffected"
    )
    svc = DefaultFirmwareUpdateService.__new__(DefaultFirmwareUpdateService)
    # Same wording, ordinary type -> ordinary guidance.
    plain = svc._create_firmware_update_exception(
        FirmwareUpdateState.WAITING_FOR_BOOTLOADER, "waiting",
        RuntimeError("does not appear to carry the PIC32 bootloader"),
    )
    typed = svc._create_firmware_update_exception(
        FirmwareUpdateState.WAITING_FOR_BOOTLOADER, "waiting",
        dedicated("does not appear to carry the PIC32 bootloader"),
    )
    assert plain.recovery_guidance != typed.recovery_guidance, (
        "identical wording with different types must select different "
        "guidance, or the selection is keyed on text"
    )
    assert "programmer" in (typed.recovery_guidance or "")


# ---- the cancel property the settle must keep ---------------------------


@pytest.mark.parametrize("settle", ["configured", "zero"])
def test_cancelling_during_the_settle_still_cancels(monkeypatch, settle):
    """The settle must remain a cancel check, including at a ZERO delay.

    ⛔ A zero post_force_boot_delay is NOT reachable through either public
    path at this base: _validate_positive rejects it when the options are
    constructed AND again when the service is constructed. The comment at
    update_service.py:579 says "which validate() allows", which is no longer
    true. The zero path still exists in the code and is exactly where
    _sleep_with_cancel IS the cancel check, so it is reached here by mutating
    the options the service already holds -- the delay is read at call time.
    """
    cancel = threading.Event()
    cancel.set()

    def _zero(service):
        service._options.post_force_boot_delay = 0.0

    exc = _run(
        monkeypatch, _never_absent(), _no_bootloader_ever(),
        options=_options(), cancel_event=cancel,
        post_construct=_zero if settle == "zero" else None,
    )
    assert exc is not None, (
        "a pre-set cancel must abort the update (settle=%s)" % settle
    )
    assert not _names_the_bootloader(exc), (
        "a cancellation must not be reported as a missing bootloader: %r" % str(exc)
    )
