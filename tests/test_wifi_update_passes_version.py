"""#270: the WiFi flash tool must be given the firmware version.

``winc_flash_tool.cmd`` finds its programmer image at
``files\\winc<device>\\<version>\\firmware\\programmer_firmware.bin``, with the
version taken from its ``/v`` argument. The default arguments template never
passed ``/v``, so with default options every update reached PROGRAMMING and
failed there with "Firmware release not found" -- after the device had already
been switched into WiFi-update mode. Measured on a board on 2026-11-16; the
same code with ``/v <version>`` added completed.

The default template now passes ``/v {version}``. The version is the one
``download_wifi_firmware`` returns with the path (``firmware_version=``) or,
when the caller passes only the path, read from the bundle's own layout. When
it can be neither, the update is refused at entry, before the device is
queried, rather than at the flash tool's late failure.

``test_default_arguments_pass_the_bundle_version`` and
``test_the_given_version_is_passed`` fail at dedc287c (no ``/v`` in the
arguments, and no ``firmware_version`` parameter).
"""
import importlib.util
from pathlib import Path

import pytest

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.update_events import FirmwareUpdateException
from daqifi.firmware.update_service import DefaultFirmwareUpdateService

_HERE = Path(__file__).parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_f = _load("fw_fakes_wifi_version",
           _HERE / "portomatic-generated" / "test_default_firmware_update_service.py")
# The bridge-exit suite owns the cmd.exe / not-WSL pins the WiFi flash path
# needs; reuse its fixture rather than a second copy that could drift.
_b = _load("fw_bridge_exit_wifi_version", _HERE / "test_wifi_update_bridge_exit.py")
_pin_cmd_exe = _b._pin_cmd_exe          # autouse fixture

_VERSION = "19.7.7"


@pytest.fixture(autouse=True)
def _short_bridge_bound(monkeypatch):
    _f._short_wifi_bridge_bound.__wrapped__(monkeypatch)


@pytest.fixture(autouse=True)
def _wsl_host(monkeypatch):
    """Take the WSL .cmd branch on every host: arguments arrive as a list."""
    import daqifi.firmware.update_service as update_service

    monkeypatch.setattr(update_service, "_is_native_windows", lambda: False)
    monkeypatch.setattr(
        update_service, "_path_for_windows_tool",
        lambda path: "C:\\WSL" + path.replace("/", "\\")
        if path.startswith("/") else path,
    )


class _RecordingDevice(_f._FakeStreamingDevice):
    """Records the version check's query as well as every send."""

    def __init__(self):
        super().__init__("fake-port-6")
        self.chip_info_queries = 0

    def get_lan_chip_info(self):
        self.chip_info_queries += 1
        return None


def _service(options=None):
    options = options if options is not None else _f._fast_options()
    options.wifi_port_override = "TESTPORT"
    runner = _f._FakeExternalProcessRunner()
    service = DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=runner,
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=options,
    )
    return service, runner


def _bundle(tmp_path, *versions):
    """A bundle laid out like the real one, holding ``versions``."""
    root = tmp_path / "wifi-firmware"
    root.mkdir()
    (root / "winc_flash_tool.cmd").write_text("@echo off")
    # Present in the real bundle and holds no programmer image: not a version.
    (root / "files" / "winc1500" / "gain_tables").mkdir(parents=True)
    for version in versions:
        _f._add_wifi_bundle_version(str(root), version)
    return root


def _tool_arguments(runner):
    arguments = runner.last_request.arguments
    assert arguments[:4] == ["/d", "/v:off", "/c", "winc_flash_tool.cmd"]
    return arguments[4:]


def _assert_refused_untouched(service, runner, device, excinfo, *fragments):
    message = str(excinfo.value)
    for fragment in fragments:
        assert fragment in message
    assert "Nothing was sent" in message
    assert device.sent_commands == []
    assert device.chip_info_queries == 0
    assert runner.last_request is None
    assert service.current_state == FirmwareUpdateState.IDLE


def test_default_arguments_pass_the_bundle_version(tmp_path):
    """Only the path is given: the version is read from the bundle."""
    service, runner = _service()
    service.update_wifi_module(_RecordingDevice(), str(_bundle(tmp_path, _VERSION)))

    assert _tool_arguments(runner) == [
        "/p", "TESTPORT", "/d", "WINC1500", "/v", _VERSION,
        "/k", "/e", "/i", "aio", "/w",
    ]
    assert service.current_state == FirmwareUpdateState.COMPLETE


def test_the_tool_file_itself_as_the_path_reads_its_bundle(tmp_path):
    service, runner = _service()
    tool = _bundle(tmp_path, _VERSION) / "winc_flash_tool.cmd"
    service.update_wifi_module(_RecordingDevice(), str(tool))

    assert _tool_arguments(runner)[4:6] == ["/v", _VERSION]


def test_the_given_version_is_passed(tmp_path):
    """The download-then-update composition: the returned version is used,
    and picks one bundle among several."""
    service, runner = _service()
    service.update_wifi_module(
        _RecordingDevice(), str(_bundle(tmp_path, "19.6.1", _VERSION)),
        firmware_version="19.6.1",
    )

    assert _tool_arguments(runner)[4:6] == ["/v", "19.6.1"]


def test_a_bundle_with_no_version_is_refused_before_anything_is_sent(tmp_path):
    service, runner = _service()
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(device, str(_bundle(tmp_path)))
    _assert_refused_untouched(
        service, runner, device, excinfo, "firmware_version=", "no files/winc*"
    )


def test_a_bundle_with_two_versions_is_refused_rather_than_guessed(tmp_path):
    service, runner = _service()
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path, "19.6.1", _VERSION))
        )
    _assert_refused_untouched(
        service, runner, device, excinfo, "19.6.1, 19.7.7", "firmware_version="
    )


def test_a_version_the_bundle_does_not_hold_is_refused(tmp_path):
    """The tool would fail with 'Firmware release not found' -- late."""
    service, runner = _service()
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path, _VERSION)), firmware_version="19.5.4"
        )
    _assert_refused_untouched(
        service, runner, device, excinfo, "'19.5.4'", "Firmware release not found"
    )


@pytest.mark.parametrize("bad", ["", "   ", 1977])
def test_an_unusable_version_is_refused(tmp_path, bad):
    service, runner = _service()
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path, _VERSION)), firmware_version=bad
        )
    _assert_refused_untouched(
        service, runner, device, excinfo, "non-empty string"
    )


def test_a_version_cmd_exe_would_interpret_is_refused(tmp_path):
    """The version reaches cmd.exe like the port and the path do."""
    service, runner = _service()
    root = _bundle(tmp_path)
    _f._add_wifi_bundle_version(str(root), "19&calc")
    device = _RecordingDevice()
    with pytest.raises(FirmwareUpdateException) as excinfo:
        service.update_wifi_module(device, str(root))
    # Refused where an unsafe port or path is: while building the request,
    # before the device is switched into update mode.
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert "firmware version '19&calc'" in str(excinfo.value.__cause__)
    assert device.sent_commands == []
    assert runner.last_request is None


def test_a_template_without_version_still_works_on_a_bundle_without_one(tmp_path):
    """A caller's own template that does not use {version} needs none."""
    options = _f._fast_options()
    options.wifi_flash_tool_arguments_template = "/p {port} /d WINC1500 /k /e /i aio /w"
    service, runner = _service(options)
    service.update_wifi_module(_RecordingDevice(), str(_bundle(tmp_path)))

    assert _tool_arguments(runner) == [
        "/p", "TESTPORT", "/d", "WINC1500", "/k", "/e", "/i", "aio", "/w",
    ]
    assert service.current_state == FirmwareUpdateState.COMPLETE


def test_a_template_with_its_own_literal_v_is_left_alone(tmp_path):
    """The #270 workaround callers already use keeps working unchanged."""
    options = _f._fast_options()
    options.wifi_flash_tool_arguments_template = (
        "/p {port} /d WINC1500 /v 19.7.7 /k /e /i aio /w"
    )
    service, runner = _service(options)
    service.update_wifi_module(_RecordingDevice(), str(_bundle(tmp_path)))

    assert _tool_arguments(runner)[4:6] == ["/v", _VERSION]


def test_the_native_windows_line_quotes_the_version(tmp_path, monkeypatch):
    import daqifi.firmware.update_service as update_service

    monkeypatch.setattr(update_service, "_is_native_windows", lambda: True)
    monkeypatch.setattr(update_service, "_path_for_windows_tool", lambda p: p)
    service, runner = _service()
    service.update_wifi_module(_RecordingDevice(), str(_bundle(tmp_path, _VERSION)))

    assert runner.last_request.arguments == (
        '/d /v:off /s /c ""winc_flash_tool.cmd" /p "TESTPORT" /d WINC1500 '
        f'/v "{_VERSION}" /k /e /i aio /w"'
    )
