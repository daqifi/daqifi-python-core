"""#190 item 5: a cancel seen after the flash tool's success line is too late.

The WiFi module flash is irreversible once the tool reports success -- that
line is what ``update_wifi_module`` treats as the authority, in preference to
the exit code. What follows it is not more flashing: it is leaving the
USB-transparent bridge and restoring the LAN configuration, i.e. the steps that
put the board back in a usable state.

So honouring a cancel observed after the success line did two wrong things at
once: it reported FAILED for an update that had in fact completed, and it
skipped the bridge exit, leaving the device transparent with its LAN down.

These tests use the same fakes and virtual clock as
``test_wifi_update_bridge_exit.py`` -- the device model, the HID transport, the
external-process runner -- so the flow exercised is the real one and nothing is
shortened to make a test pass.
"""

import importlib.util
import os
import shutil
import threading

import pytest

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.update_events import (
    FirmwareUpdateCancelledError,
    FirmwareUpdateException,
)

_HERE = os.path.dirname(__file__)


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_HERE, relpath)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The bridge-exit suite owns the device model, the command constants and the
# `clock` fixture; reuse them rather than growing a second model that could
# drift from the firmware's behaviour.
_b = _load("fw_bridge_exit", "test_wifi_update_bridge_exit.py")
_f = _load("fw_fakes", os.path.join("portomatic-generated",
                                    "test_default_firmware_update_service.py"))

clock = _b.clock                      # fixture
_pin_cmd_exe = _b._pin_cmd_exe        # autouse fixture


class _CancellingRunner:
    """Sets the cancel event while the flash tool is 'running'.

    This is the scenario item 5 is about and the one a wrapper around the
    service cannot produce: the operator clicks cancel during the flash, and
    the flash succeeds anyway. Setting the event before the call would be a
    different (already-covered) case -- an early cancel, which must still be
    honoured.
    """

    def __init__(self, cancel_event, result):
        self._cancel = cancel_event
        self._result = result
        self.calls = 0

    def run(self, request):
        self.calls += 1
        self._cancel.set()
        if request.on_standard_output_line is not None:
            for line in self._result.standard_output_lines:
                request.on_standard_output_line(line)
        return self._result


def _run(device, runner, cancel):
    """The states observed during one update_wifi_module call against the fakes.

    `state_changed` is a single callable, not a list -- same wiring as
    test_pic32_flash_safety's observer.
    """
    from daqifi.firmware.update_service import DefaultFirmwareUpdateService

    svc = DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=runner,
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=_b._options(),
    )
    states = []
    svc.state_changed = lambda args: states.append(args.current_state)
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        svc.update_wifi_module(device, firmware_dir, cancel_event=cancel)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)
    return states


def test_a_cancel_during_the_flash_still_completes_and_restores_lan(clock) -> None:
    """THE #190-5 REGRESSION. Fails before the fix: the cancel is honoured,
    the run reports FAILED, and the device is left in the bridge."""
    cancel = threading.Event()
    device = _b._FirmwareModel(clock)
    runner = _CancellingRunner(cancel, _b._result())

    states = _run(device, runner, cancel)

    # The flash ran and the cancel really was pending by the time it returned.
    assert runner.calls == 1
    assert cancel.is_set()

    # It COMPLETED -- the outcome the device actually reached.
    assert states[-1] == FirmwareUpdateState.COMPLETE
    assert FirmwareUpdateState.FAILED not in states

    # And the steps after the success line all ran: the bridge was left and
    # LAN was restored and persisted. This is the half that matters for the
    # board's state, not just for the report.
    commands = [
        c for c in device.sent_commands
        if c not in (_b.IDN, _b.ERR_QUERY, _b.POWER_QUERY, _b.STREAM_STOP)
    ]
    assert commands == [
        _b.POWER_ON, _b.FW_UPDATE, _b.LAN_APPLY, _b.BRIDGE_EXIT,
        _b.LAN_ENABLE, _b.LAN_APPLY_SAVE,
    ]
    assert device.persisted == [True]
    assert device.final_state() == ("normal", False)


def test_a_cancel_during_a_flash_that_fails_is_still_honoured(clock) -> None:
    """The control, and the half a one-sided fix would break.

    Same timing -- cancel set while the tool runs -- but the tool never reports
    its success line. Nothing irreversible completed, so the cancel must still
    be honoured: recovery runs and the update reports FAILED. If the fix had
    simply stopped checking the cancel after the tool returns, this would wrongly
    report COMPLETE.
    """
    cancel = threading.Event()
    device = _b._FirmwareModel(clock)
    runner = _CancellingRunner(
        cancel, _b._result(lines=("begin write operation",), exit_code=0)
    )

    with pytest.raises(FirmwareUpdateException) as excinfo:
        _run(device, runner, cancel)

    assert excinfo.value.failed_state != FirmwareUpdateState.COMPLETE
    # Recovery left the board usable rather than bridged.
    assert device.final_state() == ("normal", False)


def test_a_cancelled_update_chains_a_public_cause(clock) -> None:
    """#190 item 5, second half: the chained cause is a PUBLIC type.

    A caller distinguishing "the user cancelled" from "the update failed" had
    to name a private, underscore-prefixed sentinel to do it. Now the cause is
    ``FirmwareUpdateCancelledError``, which lives beside
    ``FirmwareUpdateException`` and is importable.
    """
    cancel = threading.Event()
    device = _b._FirmwareModel(clock)
    real_send = device.send

    def _send(message, on_written=None):
        real_send(message)
        if (getattr(message, "data", None) or str(message)) == _b.FW_UPDATE:
            cancel.set()        # before the tool: an early cancel, honoured

    device.send = _send

    with pytest.raises(FirmwareUpdateException) as excinfo:
        _run(device, _f._FakeExternalProcessRunner(_b._result()), cancel)

    cause = excinfo.value.__cause__
    assert isinstance(cause, FirmwareUpdateCancelledError), repr(cause)
    # ⚠ The point is the TYPE's provenance, not merely isinstance: a private
    # subclass would satisfy the check above while still forcing a caller to
    # name an underscore-prefixed internal to catch it narrowly.
    assert not type(cause).__name__.startswith("_"), type(cause).__name__
    assert type(cause).__module__ == "daqifi.firmware.update_events"
