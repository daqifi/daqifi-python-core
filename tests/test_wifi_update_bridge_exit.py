"""#190 items 1-4: the WiFi module update and the USB-transparent bridge.

LAN:FWUpdate only requests the update (SCPI_LANFwUpdate sets
WIFI_FW_UPDATE_REQUESTED); the next LAN:APPLY enters the bridge, which forwards
the USB CDC port raw to the WINC (UsbCdc_SetTransparentMode(true)); only an
exact ``SYSTem:USB:SetTransparentMode 0`` reaches the console again
(UsbCdc.c, UNSET_TRANSPARENT_FORMS), and APPLY from READY leaves the bridge.
"""

import importlib.util
import os
import shutil
import threading
import time

import pytest

from daqifi.firmware import update_options
from daqifi.firmware.update_options import FirmwareUpdateServiceOptions
from daqifi.firmware.external_process import ExternalProcessResult
from daqifi.firmware.update_events import FirmwareUpdateException
from daqifi.firmware import update_service
from daqifi.firmware.update_service import DefaultFirmwareUpdateService

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "fw_fakes",
    os.path.join(_HERE, "portomatic-generated", "test_default_firmware_update_service.py"),
)
_f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_f)

POWER_ON = "SYSTem:POWer:STATe 1"
FW_UPDATE = "SYSTem:COMMUnicate:LAN:FWUpdate"
BRIDGE_EXIT = "SYSTem:USB:SetTransparentMode 0"
LAN_ENABLE = "SYSTem:COMMunicate:LAN:ENAbled 1"
LAN_APPLY = "SYSTem:COMMunicate:LAN:APPLY"
LAN_SAVE = "SYSTem:COMMunicate:LAN:SAVE"
SUCCESS = "Operation completed successfully"


@pytest.fixture(autouse=True)
def _pin_cmd_exe(monkeypatch):
    # Same pins as the generated suite: a fixed cmd.exe, and the host not
    # treated as WSL (whose cmd.exe can't run from a Linux path).
    monkeypatch.setattr(update_service, "_running_under_wsl", lambda: False)
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: _f._FAKE_CMD_EXE if name == "cmd.exe" else real_which(name, *a, **k),
    )


def _options():
    # Production defaults: every wait runs on the virtual clock (the clock
    # fixture), so nothing is shortened to make a test pass.
    return FirmwareUpdateServiceOptions()


def _result(lines=(SUCCESS,), exit_code=0):
    return ExternalProcessResult(
        exit_code=exit_code,
        timed_out=False,
        duration_seconds=0.01,
        standard_output_lines=tuple(lines),
        standard_error_lines=tuple(),
    )


def _update(device, runner, cancel_event=None):
    service = DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=runner,
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=_options(),
    )
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        service.update_wifi_module(device, firmware_dir, cancel_event=cancel_event)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)


class _Clock:
    """Virtual time for the update service and the device model."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(update_service, "time", c)
    return c


IDN = "*IDN?"
ERR_QUERY = "SYSTem:ERRor?"
POWER_QUERY = "SYSTem:POWer:STATe?"
STREAM_STOP = "SYSTem:STReam:STOP"


class _FirmwareModel(_f._FakeStreamingDevice):
    """A device that follows the firmware's WiFi-update transitions.

    daqifi-nyquist-firmware main, wifi_manager.c / SCPILAN.c / UsbCdc.c:
    - FWUpdate sets REQUESTED;
    - an accepted APPLY engages the apply gate and queues a REINIT. A second
      APPLY while the gate is engaged is refused with -200 in the error
      queue. The gate clears at a steady state (READY, or the end of a
      normal reinit) or after 30 s;
    - REINIT waits out any STA retry backoff (retryAfterTick) BEFORE looking
      at the flags. Then READY means leave the bridge, REQUESTED means enter
      it (READY and transparent after the WINC init), and otherwise it's an
      ordinary reinit;
    - while transparent, everything but SetTransparentMode 0 goes raw to the
      WINC, and queries get no reply.
    """

    ENTRY_DELAY = 3.0  # per instance via entry_delay=
    GATE_TIMEOUT = 30.0

    def __init__(self, clock, name="fake-port", deliver_then_raise=None, drop=None,
                 backoff_for=0.0, prompt="", powered=True, late_errors=False,
                 entry_delay=None):
        super().__init__(name)
        self.clock = clock
        self.state = "normal"
        self.transparent = False
        self.ready_at = None
        self.reinit_pending = False
        self.gate_until = None
        self.backoff_until = clock.now + backoff_for
        self.errors = []
        self.refused_applies = 0
        self.raw_to_winc = []
        self._deliver_then_raise = deliver_then_raise
        self._drop = drop
        self._replies = None
        self._prompt = prompt
        self.powered = powered
        self.power_reply = True
        self.split_next_exit = False
        if entry_delay is not None:
            self.ENTRY_DELAY = entry_delay
        self._late_errors = late_errors
        self._refused_in_this_exchange = False

    def _tick(self):
        now = self.clock.now
        if self.gate_until is not None and now >= self.gate_until:
            self.gate_until = None
        if self.reinit_pending and now >= self.backoff_until:
            self.reinit_pending = False
            if self.state == "ready":
                self.state, self.transparent, self.gate_until = "normal", False, None
            elif self.state == "requested":
                self.state, self.ready_at = "entering", now + self.ENTRY_DELAY
            else:
                self.gate_until = None
        if self.state == "entering" and now >= self.ready_at:
            self.state, self.transparent, self.gate_until = "ready", True, None

    def _reply(self, line):
        if self._replies is not None:
            self._replies.append(self._prompt + line)

    def send(self, message):
        text = getattr(message, "data", None) or str(message)
        if text == self._drop:
            self._drop = None
            raise OSError("write failed before delivery")
        self.sent_commands.append(text)
        self._tick()
        if self.transparent and text == BRIDGE_EXIT and self.split_next_exit:
            # Delivered across two receive buffers: UsbCdc_FinalizeRead only
            # matches the escape within one, so it goes to the WINC.
            self.split_next_exit = False
            self.raw_to_winc.append(text)
        elif self.transparent and text != BRIDGE_EXIT:
            self.raw_to_winc.append(text)
        elif text == BRIDGE_EXIT:
            self.transparent = False
        elif text == FW_UPDATE and self.state == "normal":
            self.state = "requested"
        elif text == POWER_ON:
            self.powered = True
        elif text == POWER_QUERY:
            if self.power_reply:
                self._reply("1" if self.powered else "0")
            else:
                self._reply(POWER_QUERY)  # only the echo gets through
        elif text == LAN_APPLY:
            if not self.powered:
                pass  # UpdateNetworkSettings: no REINIT unless isPowered
            elif self.gate_until is not None:
                self.refused_applies += 1
                self.errors.append('-200,"Execution error"')
                self._refused_in_this_exchange = True
            else:
                self.gate_until = self.clock.now + self.GATE_TIMEOUT
                self.reinit_pending = True
                self._tick()
        elif text == ERR_QUERY:
            if self._late_errors and self._refused_in_this_exchange:
                # The -200 trails the APPLY's echo past the read window.
                pass
            else:
                self._reply(self.errors.pop(0) if self.errors else '0,"No error"')
        elif text == IDN:
            self._reply("DAQiFi,Nyquist1,0,3.8.0")
        if text == self._deliver_then_raise:
            self._deliver_then_raise = None
            raise OSError("flush failed after delivery")

    def execute_text_command(self, setup_action, response_timeout_ms=5000,
                             completion_timeout_ms=1000, max_total_ms=None):
        self._refused_in_this_exchange = False
        self._replies = []
        try:
            setup_action()
            return list(self._replies)
        finally:
            self._replies = None

    def final_state(self):
        self.clock.sleep(120.0)
        self._tick()
        return self.state, self.transparent


def test_a_successful_flash_enters_the_bridge_and_leaves_it_before_restoring_lan(clock) -> None:
    device = _FirmwareModel(clock)
    _update(device, _f._FakeExternalProcessRunner(_result()))
    commands = [c for c in device.sent_commands if c not in (IDN, ERR_QUERY, POWER_QUERY, STREAM_STOP)]
    assert commands == [
        POWER_ON, FW_UPDATE, LAN_APPLY, BRIDGE_EXIT, LAN_ENABLE, LAN_APPLY, LAN_SAVE,
    ]
    assert device.final_state() == ("normal", False)
    # The probe that saw the console go silent is the only thing the WINC got.
    assert set(device.raw_to_winc) <= {IDN}


@pytest.mark.parametrize(
    "result",
    [
        _result(lines=(), exit_code=0),
        _result(lines=("begin write operation", "Error: could not open port"), exit_code=0),
    ],
    ids=["exit-0-no-output", "exit-0-other-failure-line"],
)
def test_success_needs_the_tools_own_success_line(clock, result) -> None:
    device = _FirmwareModel(clock)
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(result))
    assert SUCCESS in str(excinfo.value.__cause__)
    assert device.final_state() == ("normal", False)


def test_a_failed_flash_leaves_the_bridge_without_persisting(clock) -> None:
    device = _FirmwareModel(clock)
    with pytest.raises(FirmwareUpdateException):
        _update(device, _f._FakeExternalProcessRunner(_result(lines=("x",), exit_code=1)))
    assert device.final_state() == ("normal", False)
    assert LAN_SAVE not in device.sent_commands
    assert device.is_connected


def test_a_cancel_between_fwupdate_and_apply_does_not_leave_it_bridged(clock) -> None:
    # Audit r1 (1): recovery sent T0 then APPLY, and from REQUESTED that
    # APPLY enters the bridge.
    cancel = threading.Event()
    device = _FirmwareModel(clock)
    real_send = device.send

    def _send(message):
        real_send(message)
        if (getattr(message, "data", None) or str(message)) == FW_UPDATE:
            cancel.set()

    device.send = _send
    with pytest.raises(FirmwareUpdateException):
        _update(device, _f._FakeExternalProcessRunner(_result()), cancel_event=cancel)
    assert device.final_state() == ("normal", False)
    # Only recovery's console probes can reach the WINC.
    assert set(device.raw_to_winc) <= {IDN}


@pytest.mark.parametrize("command", [FW_UPDATE, LAN_APPLY])
def test_a_send_that_raises_after_delivery_is_still_recovered(clock, command) -> None:
    # Audit r1 (2): the flag was armed only after send() returned.
    device = _FirmwareModel(clock, deliver_then_raise=command)
    with pytest.raises(FirmwareUpdateException):
        _update(device, _f._FakeExternalProcessRunner(_result()))
    assert device.final_state() == ("normal", False)


def test_an_apply_that_never_arrived_is_still_safe(clock) -> None:
    # Armed before the send, so recovery runs from REQUESTED.
    device = _FirmwareModel(clock, drop=LAN_APPLY)
    with pytest.raises(FirmwareUpdateException):
        _update(device, _f._FakeExternalProcessRunner(_result()))
    assert device.final_state() == ("normal", False)


def _cancel_after(device, command, cancel):
    real_send = device.send

    def _send(message):
        real_send(message)
        if (getattr(message, "data", None) or str(message)) == command:
            cancel.set()

    device.send = _send


def test_a_cancel_before_apply_on_a_board_in_wifi_backoff_ends_normal(clock) -> None:
    # Audit r2: in STA retry backoff the REINIT, and with it the bridge
    # entry, waits up to 30 s, and the apply gate refuses later APPLYs with
    # -200. Open-loop rounds finished first and the bridge came up after.
    cancel = threading.Event()
    device = _FirmwareModel(clock, backoff_for=25.0)
    _cancel_after(device, FW_UPDATE, cancel)
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(_result()), cancel_event=cancel)
    assert device.final_state() == ("normal", False)
    assert "power-cycle it" not in str(excinfo.value)


def test_an_apply_refused_by_the_gate_is_retried_and_recovery_ends_normal(clock) -> None:
    # Another APPLY still in progress: the gate refuses ours with -200 until
    # it clears. Then the tool fails and recovery runs.
    device = _FirmwareModel(clock, backoff_for=25.0)
    device.gate_until = clock.now + 20.0
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(_result(lines=("x",), exit_code=1)))
    assert device.refused_applies > 0  # the gate path was exercised
    assert "exited with code 1" in str(excinfo.value.__cause__)
    assert device.final_state() == ("normal", False)


class _DeadConsole(_FirmwareModel):
    def execute_text_command(self, setup_action, response_timeout_ms=5000,
                             completion_timeout_ms=1000, max_total_ms=None):
        setup_action()
        return []


def test_recovery_that_cannot_be_confirmed_is_reported(clock) -> None:
    device = _DeadConsole(clock)
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(_result(lines=("x",), exit_code=1)))
    assert "power-cycle it" in str(excinfo.value)
    assert "exited with code 1" in str(excinfo.value.__cause__)


class _StateAtLaunch(_f._FakeExternalProcessRunner):
    def __init__(self, device, result):
        super().__init__(result)
        self.device = device
        self.state_at_launch = None

    def run(self, request):
        self.device._tick()
        self.state_at_launch = (self.device.state, self.device.transparent)
        return super().run(request)


def test_on_a_board_in_wifi_backoff_the_tool_starts_only_once_the_bridge_is_up(clock) -> None:
    # Audit r3 (1): with production defaults the tool launched ~5 s after
    # APPLY while the REINIT was still deferred, and found no bridge.
    device = _FirmwareModel(clock, backoff_for=25.0)
    runner = _StateAtLaunch(device, _result())
    _update(device, runner)
    assert runner.state_at_launch == ("ready", True)
    assert device.final_state() == ("normal", False)


class _NeverBridges(_FirmwareModel):
    def _tick(self):
        super()._tick()
        if self.state == "entering":
            self.ready_at = self.clock.now + 3600.0


def test_a_bridge_that_never_comes_up_fails_before_the_tool(clock) -> None:
    device = _NeverBridges(clock)
    runner = _f._FakeExternalProcessRunner(_result())
    start = clock.now
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, runner)
    assert runner.last_request is None
    assert "did not come up" in str(excinfo.value.__cause__)
    assert clock.now - start < 3600


def test_the_recovery_watch_stops_at_the_overall_deadline(clock) -> None:
    # Audit r3 (3): the watch ignored wifi_recovery_timeout and could return
    # True after it. Here the late bridge entry needs a second watch that
    # doesn't fit in 45 s, so recovery must end unconfirmed by then.
    cancel = threading.Event()
    device = _FirmwareModel(clock, backoff_for=25.0)
    _cancel_after(device, FW_UPDATE, cancel)
    service_options = _options()
    service_options.wifi_recovery_timeout = 45.0
    service = DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=_f._FakeExternalProcessRunner(_result()),
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=service_options,
    )
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        with pytest.raises(FirmwareUpdateException) as excinfo:
            service.update_wifi_module(device, firmware_dir, cancel_event=cancel)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)
    assert "power-cycle it" in str(excinfo.value)


def test_prompt_prefixed_replies_are_read(clock) -> None:
    # Audit r3 (4): 'DAQIFI> 0,"No error"' was not recognised, so recovery
    # looped to its timeout and told the user to power-cycle.
    device = _FirmwareModel(clock, prompt="DAQIFI> ")
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(_result(lines=("x",), exit_code=1)))
    assert "power-cycle it" not in str(excinfo.value)
    assert device.final_state() == ("normal", False)


def test_a_stale_error_in_the_queue_is_not_read_as_the_applys_answer(clock) -> None:
    # The firmware leaves -200s of its own in the queue around the bridge
    # transition (bench log); one left over must not make an accepted APPLY
    # look refused and be sent again.
    device = _FirmwareModel(clock)
    device.errors.append('-200,"Execution error"')
    _update(device, _f._FakeExternalProcessRunner(_result()))
    assert device.sent_commands.count(LAN_APPLY) == 2  # prepare + restore
    assert device.final_state() == ("normal", False)


class _QuietUntilApply(_FirmwareModel):
    """Answers nothing to the first error-queue read (a slow console)."""

    def __init__(self, clock):
        super().__init__(clock)
        self._swallow_next_error_query = True

    def send(self, message):
        text = getattr(message, "data", None) or str(message)
        if text == ERR_QUERY and self._swallow_next_error_query:
            self._swallow_next_error_query = False
            self.sent_commands.append(text)
            return
        super().send(message)


def test_a_silent_drain_still_sends_the_apply(clock) -> None:
    device = _QuietUntilApply(clock)
    _update(device, _f._FakeExternalProcessRunner(_result()))
    assert device.sent_commands.count(LAN_APPLY) == 2
    assert device.final_state() == ("normal", False)


def test_a_refusal_whose_reply_is_late_is_not_taken_as_acceptance(clock) -> None:
    # Audit r4 (1): the -200 trailed the read window, "no reply" was read as
    # acceptance, and the tool launched against the SCPI console.
    device = _FirmwareModel(clock, late_errors=True)
    device.gate_until = clock.now + 10.0  # another APPLY still in progress
    runner = _StateAtLaunch(device, _result())
    _update(device, runner)
    assert device.refused_applies > 0
    assert runner.state_at_launch == ("ready", True)
    assert device.final_state() == ("normal", False)


def test_an_update_on_a_board_in_standby_is_refused_before_fwupdate(clock) -> None:
    # Audit r4 (2): in standby APPLY doesn't restart WiFi
    # (UpdateNetworkSettings' isPowered), so FWUpdate's request would linger.
    device = _FirmwareModel(clock, powered=False)
    options = _options()
    options.power_on_wifi_module_before_lan_update_mode = False
    service = DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=_f._FakeExternalProcessRunner(_result()),
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=options,
    )
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        with pytest.raises(FirmwareUpdateException) as excinfo:
            service.update_wifi_module(device, firmware_dir)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)
    assert "standby" in str(excinfo.value.__cause__)
    assert FW_UPDATE not in device.sent_commands
    assert device.state == "normal"


def test_recovery_on_a_board_that_dropped_to_standby_is_unconfirmed(clock) -> None:
    # Audit r4 (2): an APPLY in standby returns 0 without consuming the
    # request, so 40 s of answering console proved nothing.
    cancel = threading.Event()
    device = _FirmwareModel(clock)
    real_send = device.send

    def _send(message):
        real_send(message)
        if (getattr(message, "data", None) or str(message)) == FW_UPDATE:
            device.powered = False
            cancel.set()

    device.send = _send
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(_result()), cancel_event=cancel)
    assert "power-cycle it" in str(excinfo.value)


class _InterruptedTool(_f._FakeExternalProcessRunner):
    def run(self, request):
        self.last_request = request
        raise KeyboardInterrupt


def _service(runner):
    return DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=runner,
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=_options(),
    )


def test_ctrl_c_during_the_flash_tool_still_leaves_the_bridge(clock) -> None:
    # Audit r5: KeyboardInterrupt bypassed both handlers, leaving the device
    # READY and transparent and the service in PROGRAMMING.
    from daqifi.enums import FirmwareUpdateState

    device = _FirmwareModel(clock)
    service = _service(_InterruptedTool())
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        with pytest.raises(KeyboardInterrupt):
            service.update_wifi_module(device, firmware_dir)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)
    assert device.final_state() == ("normal", False)
    assert service.current_state == FirmwareUpdateState.FAILED


def test_a_second_ctrl_c_during_recovery_says_what_it_leaves(clock, caplog) -> None:
    from daqifi.enums import FirmwareUpdateState

    device = _FirmwareModel(clock)
    real_send = device.send
    exits = {"n": 0}

    def _send(message):
        if (getattr(message, "data", None) or str(message)) == BRIDGE_EXIT:
            exits["n"] += 1
            if exits["n"] == 1:
                raise KeyboardInterrupt
        real_send(message)

    device.send = _send
    service = _service(_InterruptedTool())
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        with pytest.raises(KeyboardInterrupt):
            service.update_wifi_module(device, firmware_dir)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)
    assert service.current_state == FirmwareUpdateState.FAILED
    assert "Power-cycle it" in caplog.text


def test_ctrl_c_during_recovery_after_a_failed_tool_still_ends_failed(clock, caplog) -> None:
    # Audit r6 (1): recovery started from the Exception handler; an
    # interrupt inside it escaped that handler, so the sibling BaseException
    # handler never ran and the service stayed in PROGRAMMING.
    from daqifi.enums import FirmwareUpdateState

    device = _FirmwareModel(clock)
    real_send = device.send

    def _send(message):
        if (getattr(message, "data", None) or str(message)) == BRIDGE_EXIT:
            raise KeyboardInterrupt
        real_send(message)

    device.send = _send
    service = _service(_f._FakeExternalProcessRunner(_result(lines=("x",), exit_code=1)))
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        with pytest.raises(KeyboardInterrupt):
            service.update_wifi_module(device, firmware_dir)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)
    assert service.current_state == FirmwareUpdateState.FAILED
    assert "Power-cycle it" in caplog.text


def test_an_unreadable_power_state_makes_recovery_unconfirmed(clock) -> None:
    # Audit r6 (2): an unknown power state was read as powered, and in
    # standby an APPLY doesn't consume the request.
    device = _FirmwareModel(clock)
    runner = _f._FakeExternalProcessRunner(_result(lines=("x",), exit_code=1))
    real_run = runner.run

    def _run(request):
        device.power_reply = False  # from here on only the echo comes back
        return real_run(request)

    runner.run = _run
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, runner)
    assert "power-cycle it" in str(excinfo.value)


def test_the_stream_is_stopped_before_fwupdate_even_if_untracked(clock) -> None:
    # Audit r7 (1): a stream started by a raw send leaves is_streaming
    # False, and its output kept the bridge's text exchanges alive.
    device = _FirmwareModel(clock)
    device.is_streaming = False
    _update(device, _f._FakeExternalProcessRunner(_result()))
    sent = device.sent_commands
    assert STREAM_STOP in sent and sent.index(STREAM_STOP) < sent.index(FW_UPDATE)


def test_a_device_on_tcp_is_refused_before_anything_is_sent(clock) -> None:
    # Audit r7 (2): with wifi_port_override the bridge came up on USB while
    # reconnect and recovery used the TCP transport, which can't reach it.
    device = _FirmwareModel(clock)
    device.is_usb_connection = False
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(_result()))
    assert "USB serial" in str(excinfo.value.__cause__)
    assert device.sent_commands == []


def test_a_bridge_that_comes_up_during_recoverys_first_exit_is_still_left(clock) -> None:
    # Audit r9 (1): the bridge came up during the exit delay, the power
    # query went into it unanswered, and recovery stopped with the board
    # bridged and most of its budget left.
    cancel = threading.Event()
    device = _FirmwareModel(clock, entry_delay=0.05)
    _cancel_after(device, LAN_APPLY, cancel)
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(_result()), cancel_event=cancel)
    assert device.final_state() == ("normal", False)
    assert "power-cycle it" not in str(excinfo.value)


class _SplitExitAfterFlash(_f._FakeExternalProcessRunner):
    def __init__(self, device):
        super().__init__(_result())
        self.device = device

    def run(self, request):
        self.device.split_next_exit = True  # the success path's exit is lost
        return super().run(request)


def test_a_success_whose_bridge_exit_is_lost_is_not_complete(clock) -> None:
    # Audit r10 (1): success never confirmed the exit, so a lost escape left
    # the board transparent and the update reported COMPLETE.
    from daqifi.enums import FirmwareUpdateState

    device = _FirmwareModel(clock)
    service = _service(_SplitExitAfterFlash(device))
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        with pytest.raises(FirmwareUpdateException) as excinfo:
            service.update_wifi_module(device, firmware_dir)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)
    assert "did not answer" in str(excinfo.value.__cause__)
    assert service.current_state == FirmwareUpdateState.FAILED
    assert device.final_state() == ("normal", False)  # recovery left it


def test_a_failure_before_update_mode_sends_no_bridge_exit(clock, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    device = _FirmwareModel(clock)
    with pytest.raises(FirmwareUpdateException):
        _update(device, _f._FakeExternalProcessRunner(_result()))
    assert device.sent_commands == []


def test_a_device_that_never_returns_keeps_the_original_failure(clock) -> None:
    device = _FirmwareModel(clock)
    device.connect_failures_before_success = 10**6
    with pytest.raises(FirmwareUpdateException) as excinfo:
        _update(device, _f._FakeExternalProcessRunner(_result(lines=(), exit_code=1)))
    assert "exited with code 1" in str(excinfo.value.__cause__)
    assert BRIDGE_EXIT not in device.sent_commands


def test_power_on_can_be_turned_off(clock) -> None:
    device = _FirmwareModel(clock)
    options = _options()
    options.power_on_wifi_module_before_lan_update_mode = False
    service = DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=_f._FakeExternalProcessRunner(_result()),
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=options,
    )
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        service.update_wifi_module(device, firmware_dir)
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)
    assert [c for c in device.sent_commands if c not in (IDN, ERR_QUERY, POWER_QUERY, STREAM_STOP)][:2] == [FW_UPDATE, LAN_APPLY]


@pytest.mark.parametrize(
    "field, value",
    [
        ("power_on_wifi_module_settle_delay", -1.0),
        ("post_lan_disconnect_port_release_delay", -0.1),
        ("post_usb_transparent_mode_exit_delay", -0.1),
        ("wifi_flash_success_marker", " "),
        ("wifi_recovery_timeout", 39.0),
    ],
)
def test_options_reject_invalid_bridge_settings(field, value) -> None:
    options = FirmwareUpdateServiceOptions()
    setattr(options, field, value)
    with pytest.raises(ValueError, match=field):
        options.validate()


def test_the_bridge_watch_is_not_an_option() -> None:
    # Audit r3 (2): a watch shorter than the firmware's REINIT deferral plus
    # bridge entry confirms a still-pending request as recovered.
    assert update_options.WIFI_BRIDGE_SETTLE_SECONDS >= 30.0 + 6.0
    assert not hasattr(FirmwareUpdateServiceOptions(), "wifi_recovery_settle_time")


def test_zero_delays_are_allowed() -> None:
    options = _options()
    options.power_on_wifi_module_settle_delay = 0.0
    options.post_lan_disconnect_port_release_delay = 0.0
    options.post_usb_transparent_mode_exit_delay = 0.0
    options.validate()
