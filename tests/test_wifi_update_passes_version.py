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
        service, runner, device, excinfo, "firmware_version=", "holds no"
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
    """A discovered version outside the allow-list is refused at entry; it
    used to reach the per-value cmd.exe guard while building the request."""
    service, runner = _service()
    root = _bundle(tmp_path)
    _f._add_wifi_bundle_version(str(root), "19&calc")
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(device, str(root))
    _assert_refused_untouched(
        service, runner, device, excinfo, "'19&calc'", "may contain only"
    )


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


# ---- Audit r1 on PR #274: the image the tool will open must exist ----------


def test_a_given_version_with_no_programmer_image_at_all_is_refused(tmp_path):
    """r1 finding 1: an EMPTY listing skipped the check, so a given version
    went through, FWUpdate was sent and the tool failed in PROGRAMMING."""
    service, runner = _service()
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path)), firmware_version=_VERSION
        )
    _assert_refused_untouched(
        service, runner, device, excinfo, "'19.7.7'", "Firmware release not found"
    )


def test_a_version_for_another_device_is_not_picked_up(tmp_path):
    """r1 finding 2: discovery pooled every files/winc*/ folder, so a bundle
    holding only winc3400/1.0.0 ran /d WINC1500 /v 1.0.0 and failed late."""
    service, runner = _service()
    device = _RecordingDevice()
    root = _bundle(tmp_path)
    _f._add_wifi_bundle_version(str(root), "1.0.0", device="winc3400")
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(device, str(root))
    _assert_refused_untouched(
        service, runner, device, excinfo, "/d WINC1500", "holds no"
    )


def test_a_given_version_for_another_device_is_refused(tmp_path):
    service, runner = _service()
    device = _RecordingDevice()
    root = _bundle(tmp_path)
    _f._add_wifi_bundle_version(str(root), "1.0.0", device="winc3400")
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(device, str(root), firmware_version="1.0.0")
    _assert_refused_untouched(
        service, runner, device, excinfo, "winc1500", "Firmware release not found"
    )


def test_a_custom_device_reads_its_own_folder(tmp_path):
    """The caller's /d is honoured: WINC3400 reads files/winc3400."""
    options = _f._fast_options()
    options.wifi_flash_tool_arguments_template = (
        "/p {port} /d WINC3400 /v {version} /k /e /i aio /w"
    )
    service, runner = _service(options)
    root = _bundle(tmp_path, _VERSION)
    _f._add_wifi_bundle_version(str(root), "1.4.2", device="winc3400")
    service.update_wifi_module(_RecordingDevice(), str(root))

    assert _tool_arguments(runner)[2:6] == ["/d", "WINC3400", "/v", "1.4.2"]


@pytest.mark.parametrize("template,fragment", [
    # #274 r2 audit schedule: the tool parses PAST a non-option, so the
    # second /v wins and it reads 0.0.0 while 19.7.7 was validated.
    ("/p {port} /d WINC1500 /v {version} stray /v 0.0.0", "'stray'"),
    ("/p {port} /d WINC1500 /v {version} /e /v 0.0.0", "it has 2 /v"),
    ("/p {port} /v {version} /w", "it has 0 /d"),
    ("/p {port} /d WINC1500 /d WINC3400 /v {version}", "it has 2 /d"),
    ("/p {port} stray /d WINC1500 /v {version}", "'stray'"),
    ("/p {port} /d {port} /v {version}", "not a literal device"),
    ("/p {port} /d WINC1500 /v x{version}", "not exactly {version}"),
    # An option right before /v can take /v as its value: the tool's
    # unknown /k does, and never reads the version.
    ("/p {port} /d WINC1500 /k /v {version} /w", "'/k' comes right before '/v'"),
    ("/p {port} /e /d WINC1500 /v {version}", "'/e' comes right before '/d'"),
])
def test_a_template_of_another_shape_is_refused(tmp_path, template, fragment):
    options = _f._fast_options()
    options.wifi_flash_tool_arguments_template = template
    service, runner = _service(options)
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path, _VERSION)), firmware_version=_VERSION
        )
    _assert_refused_untouched(service, runner, device, excinfo, fragment)


def test_options_are_matched_case_insensitively(tmp_path):
    options = _f._fast_options()
    options.wifi_flash_tool_arguments_template = "-P {port} /D WINC3400 -V {version} /w"
    service, runner = _service(options)
    root = _bundle(tmp_path)
    _f._add_wifi_bundle_version(str(root), "1.4.2", device="winc3400")
    service.update_wifi_module(_RecordingDevice(), str(root))

    assert _tool_arguments(runner) == [
        "-P", "TESTPORT", "/D", "WINC3400", "-V", "1.4.2", "/w",
    ]


def test_the_downloaded_version_drives_the_update(tmp_path):
    """#274 r2: download_wifi_firmware returned the release tag, so a tag
    "v19.5.4" over files/winc1500/19.5.4 was refused, contradicting "pass the
    version it returns". The documented composition, end to end."""
    d = _load("download_fakes_wifi_version",
              _HERE / "portomatic-generated" / "test_download_service.py")
    from daqifi.firmware.github_download_service import (
        GitHubFirmwareDownloadService,
    )

    urlopen, _ = d._serve({d._WIFI_ZIP_URL: d._wifi_zip()})
    path, version = d._run(
        GitHubFirmwareDownloadService().download_wifi_firmware(tmp_path),
        urlopen=urlopen,
    )
    service, runner = _service()
    service.update_wifi_module(
        _RecordingDevice(), str(path), firmware_version=version
    )

    assert _tool_arguments(runner)[4:6] == ["/v", "19.5.4"]
    assert service.current_state == FirmwareUpdateState.COMPLETE


def test_a_version_cmd_exe_would_split_is_refused(tmp_path):
    """cmd.exe splits a batch script's arguments at , ; = too, so the tool
    would read only "19" of a folder named "19,7"."""
    service, runner = _service()
    device = _RecordingDevice()
    root = _bundle(tmp_path)
    _f._add_wifi_bundle_version(str(root), "19,7")
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(device, str(root))
    _assert_refused_untouched(service, runner, device, excinfo, "may contain only")


# ---- Audit r3 on PR #274: allow-lists, not a model of cmd.exe -------------


def test_a_comma_joined_second_v_is_refused(tmp_path):
    """r3 schedule 1: "/v,19.6.1" passed the shape as one unknown option, but
    cmd.exe splits it at the comma into /v 19.6.1, which overrides the
    validated 19.7.7 and flashes the other image."""
    options = _f._fast_options()
    options.wifi_flash_tool_arguments_template = (
        "/p {port} /d WINC1500 /v {version} /e /v,19.6.1 /w"
    )
    service, runner = _service(options)
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path, "19.6.1", _VERSION)),
            firmware_version=_VERSION,
        )
    _assert_refused_untouched(service, runner, device, excinfo, "'/v,19.6.1'")


def test_a_version_delayed_expansion_would_rewrite_is_refused(tmp_path, monkeypatch):
    """r3 schedule 2: winc_flash_tool.cmd enables delayed expansion, so
    "19.7.7!candidate!" reaches it as 19.7.7 and a different image is
    flashed. Native Windows, where the per-value cmd.exe guard allows '!'."""
    import daqifi.firmware.update_service as update_service

    monkeypatch.setattr(update_service, "_is_native_windows", lambda: True)
    monkeypatch.setattr(update_service, "_path_for_windows_tool", lambda p: p)
    service, runner = _service()
    device = _RecordingDevice()
    root = _bundle(tmp_path, _VERSION)
    _f._add_wifi_bundle_version(str(root), "19.7.7!candidate!")
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(root), firmware_version="19.7.7!candidate!"
        )
    _assert_refused_untouched(
        service, runner, device, excinfo, "'19.7.7!candidate!'", "may contain only"
    )


# One template argument per character class outside the allow-list. The
# arguments are written as cmd.exe's splitter needs them to arrive as shown.
@pytest.mark.parametrize("written,arrives", [
    ("a,b", "a,b"),        # argument separators
    ("a;b", "a;b"),
    ("a=b", "a=b"),
    ("a!b!", "a!b!"),      # delayed expansion
    ("a%b%", "a%b%"),      # expansion
    ("a^^b", "a^b"),       # escape
    ("a&b", "a&b"),        # command separators and redirection
    ("a|b", "a|b"),
    ("a<b", "a<b"),
    ("a>b", "a>b"),
    ("a'b", "a'b"),        # quote
    ('"a b"', "a b"),      # whitespace
    ("a\\b", "a\\b"),  # backslash
])
def test_a_template_argument_outside_the_allow_list_is_refused(
    tmp_path, written, arrives
):
    options = _f._fast_options()
    options.wifi_flash_tool_arguments_template = (
        f"/p {{port}} /d WINC1500 /v {{version}} /i {written} /w"
    )
    service, runner = _service(options)
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path, _VERSION)), firmware_version=_VERSION
        )
    _assert_refused_untouched(service, runner, device, excinfo, repr(arrives))


@pytest.mark.parametrize("version", [
    "19,7", "19;7", "19=7", "19!7", "19%7", "19^7", "19&7", "19 7",
    '19"7', "19'7", "19/7", "19\\7", "19:7",
])
def test_a_given_version_outside_the_allow_list_is_refused(tmp_path, version):
    service, runner = _service()
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path, _VERSION)), firmware_version=version
        )
    _assert_refused_untouched(
        service, runner, device, excinfo, repr(version), "may contain only"
    )


@pytest.mark.parametrize("device_name", ["WINC:1500", "WINC/1500", "{port}"])
def test_a_device_outside_the_allow_list_is_refused(tmp_path, device_name):
    options = _f._fast_options()
    options.wifi_flash_tool_arguments_template = (
        f"/p {{port}} /d {device_name} /v {{version}} /w"
    )
    service, runner = _service(options)
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path, _VERSION)), firmware_version=_VERSION
        )
    _assert_refused_untouched(
        service, runner, device, excinfo, repr(device_name), "literal device name"
    )


# ---- r4 on PR #274: the substituted {port} is allow-listed too ------------


@pytest.mark.parametrize("port", [
    # cmd.exe splits the unquoted WSL argument at the commas into a second
    # /v that overrides the validated version.
    "COMx,/v,0.0.0",
    # winc_flash_tool.cmd enables delayed expansion.
    "COMx!y!",
])
def test_a_port_outside_the_allow_list_is_refused(tmp_path, port):
    options = _f._fast_options()
    service, runner = _service(options)
    options.wifi_port_override = port
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path, _VERSION)), firmware_version=_VERSION
        )
    _assert_refused_untouched(service, runner, device, excinfo, repr(port))


@pytest.mark.parametrize("port", ["COMx", "/dev/ttyACMx", "tty.usbmodem-x.y"])
def test_ordinary_port_names_pass_the_allow_list(tmp_path, port):
    options = _f._fast_options()
    service, runner = _service(options)
    options.wifi_port_override = port
    service.update_wifi_module(
        _RecordingDevice(), str(_bundle(tmp_path, _VERSION)),
        firmware_version=_VERSION,
    )
    assert _tool_arguments(runner)[:2] == ["/p", port]


def test_a_delayed_expansion_port_is_refused_on_native_windows(tmp_path, monkeypatch):
    """Native Windows quotes each value and its per-value guard checks only
    % and ", so before the allow-list a '!' port reached the tool, whose
    delayed expansion rewrites it."""
    import daqifi.firmware.update_service as update_service

    monkeypatch.setattr(update_service, "_is_native_windows", lambda: True)
    monkeypatch.setattr(update_service, "_path_for_windows_tool", lambda p: p)
    options = _f._fast_options()
    service, runner = _service(options)
    options.wifi_port_override = "COMx!y!"
    device = _RecordingDevice()
    with pytest.raises(ValueError) as excinfo:
        service.update_wifi_module(
            device, str(_bundle(tmp_path, _VERSION)), firmware_version=_VERSION
        )
    _assert_refused_untouched(service, runner, device, excinfo, "'COMx!y!'")
