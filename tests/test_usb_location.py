"""daqifi._internal.usb_location against a fake sysfs tree."""
from __future__ import annotations

import os
import sys

import pytest

from daqifi._internal.usb_location import hid_device_location, serial_port_location

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="sysfs locations are Linux-only"
)


def _link(root, class_name, node, target):
    device_dir = os.path.join(root, target.lstrip("/"))
    os.makedirs(device_dir, exist_ok=True)
    class_dir = os.path.join(root, "class", class_name, node)
    os.makedirs(class_dir, exist_ok=True)
    os.symlink(device_dir, os.path.join(class_dir, "device"))


def test_cdc_tty_and_its_bootloader_share_a_location(tmp_path):
    root = str(tmp_path)
    port = "devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2.3"
    _link(root, "tty", "ttyACM0", f"{port}/1-2.3:1.0")
    _link(root, "hidraw", "hidraw4", f"{port}/1-2.3:1.0/0003:04D8:003C.0007")

    assert serial_port_location("/dev/ttyACM0", root) == "1-2.3"
    assert hid_device_location(b"/dev/hidraw4", root) == "1-2.3"


def test_boards_on_different_ports_differ(tmp_path):
    root = str(tmp_path)
    _link(root, "hidraw", "hidraw0", "devices/pci0000:00/usb1/1-1/1-1:1.0/0003:04D8:003C.0001")
    _link(root, "hidraw", "hidraw1", "devices/pci0000:00/usb1/1-4/1-4:1.0/0003:04D8:003C.0002")
    assert hid_device_location("/dev/hidraw0", root) == "1-1"
    assert hid_device_location("/dev/hidraw1", root) == "1-4"


def test_usbip_virtual_ports_are_unknown(tmp_path):
    # WSL2 + usbipd: the vhci port can change when the board re-attaches
    # after FORceBoot, so it is no evidence of identity.
    root = str(tmp_path)
    _link(root, "tty", "ttyACM0", "devices/platform/vhci_hcd.0/usb1/1-1/1-1:1.0")
    assert serial_port_location("/dev/ttyACM0", root) is None


@pytest.mark.parametrize(
    "path", [None, "", "COM3", "/dev/ttyACM9", b"1-1:1.0", "\\\\?\\hid#vid_04d8"]
)
def test_unknowable_locations_are_none(tmp_path, path):
    assert serial_port_location(path if isinstance(path, str) else None, str(tmp_path)) is None
    assert hid_device_location(path, str(tmp_path)) is None


def test_by_id_alias_resolves_to_its_tty(tmp_path):
    # Audit finding on #165: /dev/serial/by-id/... aliases bypassed the
    # lookup (no such name under /sys/class/tty), disabling port binding.
    root = str(tmp_path / "sys")
    _link(root, "tty", "ttyACM0", "devices/pci0000:00/usb1/1-2/1-2:1.0")
    node = tmp_path / "dev" / "ttyACM0"
    node.parent.mkdir()
    node.write_text("")
    alias = tmp_path / "by-id" / "usb-DAQiFi_Nyquist-if00"
    alias.parent.mkdir()
    alias.symlink_to(node)

    assert serial_port_location(str(alias), root) == "1-2"
