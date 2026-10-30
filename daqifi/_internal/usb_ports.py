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
from typing import Callable, Dict, List, Optional, Set, TypeVar

__all__ = [
    "DAQIFI_APPLICATION_PRODUCT_ID",
    "DAQIFI_VENDOR_ID",
    "daqifi_serial_ports",
    "list_usb_serial_ports",
    "run_port_probe",
]

DAQIFI_VENDOR_ID = 0x04D8
DAQIFI_APPLICATION_PRODUCT_ID = 0xF794

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


# Live probe threads per port: registered before a probe starts and removed
# when it ends, so a running probe (on time or abandoned) is always known.
_live: Dict[str, Set[threading.Thread]] = {}
_live_lock = threading.Lock()


def run_port_probe(
    port: str,
    probe: Callable[[str, threading.Event], Optional[T]],
    timeout_s: float,
) -> Optional[T]:
    """``probe(port)``, or None if it takes longer than ``timeout_s`` or
    another probe of ``port`` is still running.

    ``timeout_s`` must exceed the probe's own worst healthy duration (its
    open, waits and read timeouts), or a slow but healthy device would be
    discarded; each caller derives it from its probe's bounds. The probe
    runs on a daemon thread; one that overruns is abandoned (a blocked open
    or read can't be interrupted from outside) and stays registered until
    it ends, so a wedged port costs one stuck thread and isn't probed again
    meanwhile, by this caller or a concurrent one. An exception in the
    probe gives None.

    The probe gets an event that is set when it is abandoned, and must not
    act on the device once it is (check it after the open and before every
    write): a late open must not send STOP or a format change that would
    break a newer session on the same board.
    """
    result: List[Optional[T]] = [None]
    cancelled = threading.Event()

    def _run() -> None:
        try:
            result[0] = probe(port, cancelled)
        except Exception:
            result[0] = None
        finally:
            with _live_lock:
                threads = _live.get(port)
                if threads is not None:
                    threads.discard(thread)
                    if not threads:
                        del _live[port]

    thread = threading.Thread(target=_run, name=f"daqifi-probe-{port}", daemon=True)
    with _live_lock:
        running = _live.setdefault(port, set())
        if running:
            return None
        running.add(thread)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        cancelled.set()
        return None
    return result[0]
