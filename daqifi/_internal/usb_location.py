"""Physical USB location of a serial port or HID device, where knowable.

A DAQiFi board re-enumerates into its HID bootloader on the same physical
USB port its application's CDC serial port used, so the port path ties the
bootloader to the board that was told to reboot into it. Upstream
daqifi-core binds the same way (``IUsbLocationProvider`` / location keys).

Only Linux exposes this without platform APIs: ``/sys/class/<class>/<name>/
device`` resolves into ``.../usb<bus>/<bus>-<port>[.<port>...]/...``. That
``<bus>-<ports>`` component is the location. Elsewhere, and for devices on
``vhci_hcd`` (usbip, i.e. WSL2 + usbipd, where the virtual port can change
when the board re-attaches), the location is reported as unknown (None) and
callers must not treat it as a match.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Optional, Union

__all__ = ["canonical_port_name", "hid_device_location", "serial_port_location"]

_USB_DEVICE_DIR = re.compile(r"^\d+-\d+(\.\d+)*$")
_SYSFS_ROOT = "/sys"


def _usb_port_path(class_name: str, node: str, sysfs_root: str) -> Optional[str]:
    if not sys.platform.startswith("linux"):
        return None
    link = os.path.join(sysfs_root, "class", class_name, node, "device")
    if not os.path.exists(link):
        return None
    resolved = os.path.realpath(link)
    parts = resolved.split(os.sep)
    if any(part.startswith("vhci_hcd") for part in parts):
        return None
    ports = [part for part in parts if _USB_DEVICE_DIR.match(part)]
    return ports[-1] if ports else None


def canonical_port_name(port_name: Optional[str]) -> Optional[str]:
    """Resolve a POSIX alias such as ``/dev/serial/by-id/...`` to its node.

    Port listings and ``/sys/class/tty`` use the node name (``ttyACM0``), so
    an alias must be resolved before comparing against either. Windows names
    (``COM3``) and anything not an absolute POSIX path pass through: there,
    ``realpath`` would join them onto the working directory.
    """
    if not port_name or not port_name.startswith("/") or os.name != "posix":
        return port_name
    return os.path.realpath(port_name)


def serial_port_location(
    port_name: Optional[str], sysfs_root: str = _SYSFS_ROOT
) -> Optional[str]:
    """``"<bus>-<ports>"`` for a Linux USB tty such as ``/dev/ttyACM0``, else None."""
    port_name = canonical_port_name(port_name)
    if not port_name:
        return None
    return _usb_port_path("tty", os.path.basename(port_name), sysfs_root)


def hid_device_location(
    device_path: Union[str, bytes, None], sysfs_root: str = _SYSFS_ROOT
) -> Optional[str]:
    """``"<bus>-<ports>"`` for a hidapi hidraw path such as ``/dev/hidraw0``, else None."""
    if isinstance(device_path, bytes):
        device_path = device_path.decode("utf-8", "replace")
    if not device_path or not device_path.startswith("/dev/hidraw"):
        return None
    return _usb_port_path("hidraw", os.path.basename(device_path), sysfs_root)
