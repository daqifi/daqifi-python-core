"""DAQiFi serial ports on this host, and probing them safely.

A DAQiFi board in its application enumerates as a USB CDC-ACM serial port
with VID/PID 04D8:F794. Discovery and the firmware updater list only those
ports (plus CDC-ACM ports whose USB IDs can't be read), so nothing writes
SCPI to an unrelated device. Upstream daqifi-core filters the same way
(``DaqifiUsbIds``, daqifi/daqifi-core#201).

Probing a port can hang outright: opening a wedged driver, or a read that
never returns. :func:`run_port_probe` bounds each probe by a hard timeout and
never starts a second probe on a port whose abandoned one is still running
(upstream daqifi/daqifi-core#295).
"""
from __future__ import annotations

import os
import re
import threading
from typing import Callable, Dict, List, Optional, TypeVar

__all__ = [
    "DAQIFI_APPLICATION_PRODUCT_ID",
    "DAQIFI_VENDOR_ID",
    "PORT_PROBE_HARD_TIMEOUT_S",
    "daqifi_serial_ports",
    "list_usb_serial_ports",
    "run_port_probe",
]

DAQIFI_VENDOR_ID = 0x04D8
DAQIFI_APPLICATION_PRODUCT_ID = 0xF794

# A probe opens the port, wakes the device and waits for its reply: about
# 2 s here. Upstream allows 3 s per port; a probe past that is abandoned.
PORT_PROBE_HARD_TIMEOUT_S = 3.0

# USB CDC-ACM serial nodes, the class a DAQiFi board enumerates as (Linux
# and macOS names). A built-in UART (ttyS0, COM1) has no USB IDs either but
# can't be a board.
_CDC_ACM_NODE = re.compile(r"^(ttyACM\d+|(cu|tty)\.usbmodem.*)$")

T = TypeVar("T")


def list_usb_serial_ports(vendor_id: int, product_id: int) -> Optional[List[str]]:
    """Serial ports of USB devices with this VID/PID, or None when they
    can't be listed.

    A CDC-ACM port whose USB IDs can't be read (sysfs hidden, e.g. in a
    container) is included: it may be a board, and leaving it out would let
    two boards pass as none. A device is listed once: on macOS its
    ``/dev/cu.*`` node, not also its ``/dev/tty.*`` one.
    """
    try:
        from serial.tools import list_ports

        ports = {
            port.device
            for port in list_ports.comports()
            if (port.vid, port.pid) == (vendor_id, product_id)
            or (
                port.vid is None
                and _CDC_ACM_NODE.match(os.path.basename(port.device or ""))
            )
        }
    except Exception:
        return None
    # macOS has a /dev/tty.* and a /dev/cu.* node per device: keep cu.
    return sorted(
        p for p in ports if not (p.startswith("/dev/tty.") and "/dev/cu." + p[9:] in ports)
    )


def daqifi_serial_ports() -> List[str]:
    """Serial ports that may be a DAQiFi board in its application; empty
    when the ports can't be listed."""
    return list_usb_serial_ports(DAQIFI_VENDOR_ID, DAQIFI_APPLICATION_PRODUCT_ID) or []


# Ports whose probe outlived its timeout, with the thread still running it.
_abandoned: Dict[str, threading.Thread] = {}
_abandoned_lock = threading.Lock()


def run_port_probe(
    port: str,
    probe: Callable[[str], Optional[T]],
    timeout_s: Optional[float] = None,
) -> Optional[T]:
    """``probe(port)``, or None if it takes longer than ``timeout_s``
    (default :data:`PORT_PROBE_HARD_TIMEOUT_S`).

    The probe runs on a daemon thread; one that overruns is abandoned (a
    blocked open or read can't be interrupted from outside) and its port is
    skipped by later calls until that thread finishes, so a wedged port
    costs at most one stuck thread. An exception in the probe gives None.
    """
    with _abandoned_lock:
        stuck = _abandoned.get(port)
        if stuck is not None:
            if stuck.is_alive():
                return None
            del _abandoned[port]

    result: List[Optional[T]] = [None]

    def _run() -> None:
        try:
            result[0] = probe(port)
        except Exception:
            result[0] = None

    thread = threading.Thread(target=_run, name=f"daqifi-probe-{port}", daemon=True)
    thread.start()
    thread.join(PORT_PROBE_HARD_TIMEOUT_S if timeout_s is None else timeout_s)
    if thread.is_alive():
        with _abandoned_lock:
            _abandoned[port] = thread
        return None
    return result[0]
