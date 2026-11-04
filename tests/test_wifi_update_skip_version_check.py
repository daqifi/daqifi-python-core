"""#190: `skip_version_check` lets an up-to-date WiFi module be reflashed.

``update_wifi_module`` returns early, reporting COMPLETE with "already up to
date", when the module's own version matches the latest release. That is right
by default -- reflashing an unchanged image is a pointless risk.

It also means an up-to-date module cannot be reflashed AT ALL, and that is a
problem for validating the WiFi update path itself: there is one published
image, and a bench whose boards are all at or past it has no way to exercise
the flash. The only way to prove the path works on hardware is to reinstall the
current version deliberately.

So `skip_version_check` mirrors ``skipVersionCheck`` on the C#
``UpdateWifiModuleAsync``. It overrides the VERSION CHECK and nothing else --
every other refusal on the path still applies, which the last test here pins.
"""

import importlib.util
import os
import shutil
import threading

import pytest

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.lan_chip_info import LanChipInfo
from daqifi.firmware.release_info import FirmwareReleaseInfo
from daqifi.firmware.update_service import DefaultFirmwareUpdateService
from daqifi.firmware.version import FirmwareVersion

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "fw_fakes",
    os.path.join(_HERE, "portomatic-generated",
                 "test_default_firmware_update_service.py"),
)
_f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_f)

# The bridge-exit suite owns the cmd.exe / not-WSL pins the WiFi flash path
# needs; reuse its fixture rather than a second copy that could drift.
_bspec = importlib.util.spec_from_file_location(
    "fw_bridge_exit", os.path.join(_HERE, "test_wifi_update_bridge_exit.py")
)
_b = importlib.util.module_from_spec(_bspec)
_bspec.loader.exec_module(_b)

_pin_cmd_exe = _b._pin_cmd_exe          # autouse fixture

FW_UPDATE = "SYSTem:COMMUnicate:LAN:FWUpdate"
_CURRENT = "19.5.4"


@pytest.fixture(autouse=True)
def _short_bridge_bound(monkeypatch):
    # Same pin the generated suite uses: these fakes have no real bridge to
    # wait out, and the firmware bound would otherwise dominate the runtime.
    _f._short_wifi_bridge_bound.__wrapped__(monkeypatch)


def _up_to_date_device(name):
    """A device whose module already reports the latest published version."""
    return _f._FakeLanChipInfoStreamingDevice(
        name,
        chip_info=LanChipInfo(
            chip_id=1234, fw_version=_CURRENT, build_date="Jan  8 2019"
        ),
    )


def _service(runner=None):
    return DefaultFirmwareUpdateService(
        hid_transport=_f._FakeHidTransport(),
        firmware_download_service=_f._FakeFirmwareDownloadService(
            latest_wifi_release=FirmwareReleaseInfo(
                version=FirmwareVersion(19, 5, 4),
                tag_name=_CURRENT,
                is_pre_release=False,
            )
        ),
        external_process_runner=runner or _f._FakeExternalProcessRunner(),
        bootloader_protocol=_f._FakeBootloaderProtocol([bytes([0x10])]),
        hid_device_enumerator=_f._FakeHidEnumerator([]),
        options=_f._fast_options(),
    )


def _run(service, device, **kwargs):
    progress = []
    firmware_dir = _f._make_temp_wifi_dir()
    try:
        service.update_wifi_module(
            device, firmware_dir, progress=progress.append, **kwargs
        )
    finally:
        shutil.rmtree(firmware_dir, ignore_errors=True)
    return progress


def test_without_the_flag_an_up_to_date_module_is_not_flashed(clock=None) -> None:
    """The control: today's behaviour, unchanged.

    Here so the next change to this path has to keep it. A flag that silently
    became the default would pass every test below and fail this one.
    """
    device = _up_to_date_device("fake-port-skip-1")
    service = _service()

    progress = _run(service, device)

    assert service.current_state == FirmwareUpdateState.COMPLETE
    assert FW_UPDATE not in device.sent_commands
    complete = [p for p in progress if p.state == FirmwareUpdateState.COMPLETE]
    assert complete and "already up to date" in complete[-1].current_operation.lower()


def test_with_the_flag_an_up_to_date_module_is_flashed(clock=None) -> None:
    """THE #190 REGRESSION. Fails on main: the parameter does not exist, so
    the call raises TypeError; once it exists but is ignored, the module is
    still skipped and FWUpdate is never sent."""
    device = _up_to_date_device("fake-port-skip-2")
    runner = _f._FakeExternalProcessRunner()
    service = _service(runner)

    progress = _run(service, device, skip_version_check=True)

    # The flash actually ran: the device was asked to enter update mode and
    # the external tool was invoked.
    assert FW_UPDATE in device.sent_commands
    assert runner.last_request is not None

    # And it completed on the real path, not the "nothing to do" shortcut.
    assert service.current_state == FirmwareUpdateState.COMPLETE
    complete = [p for p in progress if p.state == FirmwareUpdateState.COMPLETE]
    assert complete
    assert "already up to date" not in complete[-1].current_operation.lower()


def test_the_flag_overrides_only_the_version_check(clock=None) -> None:
    """⛔ THE ARM IS NOT WIDER THAN THE DEFECT.

    `skip_version_check` reaches the version check and nothing else. A device
    that cannot be updated for any OTHER reason must still be refused with the
    flag set -- here, a non-USB transport, which the path refuses because the
    bridge is reachable only on the USB CDC port.

    Without this, "skip the check" could be read (or later implemented) as
    "skip the preflight", which is how an override becomes a way to flash a
    device that should never have been flashed.
    """
    device = _up_to_date_device("fake-port-skip-3")
    device.is_usb_connection = False
    service = _service()

    with pytest.raises(Exception) as excinfo:
        _run(service, device, skip_version_check=True)

    # Refused for the transport, not silently skipped as up-to-date.
    assert "USB" in str(excinfo.value) or "USB" in str(excinfo.value.__cause__)
    assert FW_UPDATE not in device.sent_commands
