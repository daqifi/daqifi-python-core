"""Hardware-test collection guard.

Every ``test_*.py`` in this directory:

* Is automatically marked ``@pytest.mark.hardware`` via the
  ``pytest_collection_modifyitems`` hook below — so
  ``pytest -m hardware`` selects them and ``pytest -m "not hardware"``
  excludes them.
* Skip-collects when no DAQiFi device is available (the parent
  ``tests/integration/conftest.py`` already does this via the
  session-scoped ``device_info`` fixture; we just need each test to
  depend on a fixture that depends on ``device_info``).

The ``serial_transport``, ``clean_device``, ``error_drainer``,
``firmware_stats_diff``, ``wire_capture`` and ``destructive_ok``
fixtures live one directory up — see
``tests/integration/conftest.py``.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path
from typing import Callable, Optional

import pytest

from daqifi.devices import DaqifiDeviceFactory
from daqifi.discovery import DeviceFinder, DeviceInfo


_HARDWARE_DIR = Path(__file__).resolve().parent

# USB CDC convention shared with the hardware test files. Baud is
# meaningless on USB CDC (the host driver ignores it) but explicit
# consistency keeps the helpers aligned with the test modules.
_USB_CDC_BAUD = 115200


@pytest.fixture
def wait_for_device_back(
    device_info: DeviceInfo,
) -> Callable[..., Optional[str]]:
    """Returns a callable for waiting for our DAQiFi device to come
    back on USB after a disruptive operation (``*RST``,
    ``SYSTem:REboot``, firmware update, etc.).

    Usage in a test::

        def test_something_that_drops_usb(
            device_facade: NyquistDevice,
            wait_for_device_back,
        ) -> None:
            device_facade.reset()  # *RST drops USB CDC
            try:
                device_facade.disconnect()  # release stale handle
            except Exception:
                pass
            new_port = wait_for_device_back()
            if new_port is None:
                pytest.skip("Device didn't return on USB within timeout")
            new_dev = NyquistDevice.connect(new_port)
            ...

    The returned callable accepts ``timeout_s`` (default 30s) and
    returns:
      - the port path the device came back on (may equal
        ``device_info.port`` or be a new path if the kernel renamed
        the device on re-enumeration), OR
      - ``None`` if the device didn't return within the timeout (e.g.
        WSL2/usbipd doesn't auto-reattach — the operator needs to run
        ``usbipd attach`` manually).

    On success, the session-scoped ``device_info.port`` is updated
    in place if the kernel renamed our device, so subsequent test
    fixtures use the correct port.

    The fixture skips the test entirely if ``DAQIFI_EXPECTED_SERIAL_NUMBER``
    isn't set — without it we can't safely identify which unit came
    back, and on a multi-unit workstation we could rebind to the wrong
    device. This matches the destructive-test guard in
    ``test_network_config.py``.
    """
    expected_sn = os.environ.get("DAQIFI_EXPECTED_SERIAL_NUMBER")
    if not expected_sn:
        pytest.skip(
            "DAQIFI_EXPECTED_SERIAL_NUMBER not set; can't safely "
            "reconnect after device disruption — refusing to bind to "
            "an arbitrary unit. Set this to your unit's SN "
            "(see .claude/hardware-rig.md)."
        )

    def _port_is_our_device(port: str) -> bool:
        # `initialize_device=False` skips connect_serial's
        # auto-initialize so the explicit `probe.initialize()` below
        # isn't redundant — lets connect-time vs init-time exceptions
        # be caught separately.
        try:
            probe = DaqifiDeviceFactory.connect_serial(
                port_name=port,
                baud_rate=_USB_CDC_BAUD,
                initialize_device=False,
            )
        except Exception:
            return False
        try:
            probe.initialize()
            # `metadata.serial_number` is populated asynchronously
            # from the protobuf status message; `initialize()` already
            # sleeps 1.0s waiting for it but under load the message
            # can arrive late. Poll briefly so a slow status frame
            # doesn't cause a false-negative SN check.
            sn_deadline = time.monotonic() + 2.0
            while time.monotonic() < sn_deadline:
                sn = probe.metadata.serial_number
                if sn:
                    return sn == expected_sn
                time.sleep(0.05)
            return False
        except Exception:
            return False
        finally:
            try:
                probe.disconnect()
            except Exception:
                pass

    def _try_usbipd_attach() -> None:
        """If running under WSL2 and DAQIFI_USBIPD_BUSID is set,
        invoke ``usbipd.exe attach --wsl --busid <BUSID>`` once to
        re-bind the device after a USB drop. usbipd doesn't
        auto-reattach after device re-enumeration, so on WSL2 a
        post-reset device is invisible to the kernel until this
        runs. No-op on Windows native (where the COM port comes
        back automatically) and on bare Linux."""
        busid = os.environ.get("DAQIFI_USBIPD_BUSID")
        if not busid:
            return
        try:
            with open("/proc/sys/kernel/osrelease") as f:
                if "microsoft" not in f.read().lower():
                    return  # not WSL
        except (FileNotFoundError, PermissionError):
            return
        try:
            import subprocess
            subprocess.run(
                ["usbipd.exe", "attach", "--wsl", "--busid", busid],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    def _wait(*, timeout_s: float = 30.0) -> Optional[str]:
        original_port = device_info.port
        deadline = time.monotonic() + timeout_s
        # `DeviceFinder.discover()` can be slow on Windows where
        # `get_serial_ports()` returns COM1..COM255 and each port is
        # probed via *IDN?. Cap discovery to once per ~5s and skip
        # the call when too little budget remains.
        last_discovery = -math.inf
        last_usbipd_attach = -math.inf
        DISCOVERY_INTERVAL_S = 5.0
        DISCOVERY_BUDGET_S = 5.0
        # Re-try usbipd attach periodically: the device drops from USB
        # a few seconds AFTER *RST returns, so a single attach at t=0
        # often misses the window. Re-attaching every ~3s catches it
        # whenever it comes back.
        USBIPD_ATTACH_INTERVAL_S = 3.0
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if _port_is_our_device(original_port):
                # Same port came back AND it's us — no update needed.
                return original_port
            now = time.monotonic()
            # Re-attach via usbipd if enough time has passed. Cheap
            # no-op on Windows native; only WSL2 with the env var
            # set actually invokes the subprocess.
            if now - last_usbipd_attach >= USBIPD_ATTACH_INTERVAL_S:
                _try_usbipd_attach()
                last_usbipd_attach = now
            if now - last_discovery < DISCOVERY_INTERVAL_S:
                continue
            if deadline - now < DISCOVERY_BUDGET_S:
                # Not enough time left to safely run a discovery scan.
                break
            last_discovery = now
            for candidate in DeviceFinder.discover():
                if candidate.port == original_port:
                    continue  # already tried above
                if _port_is_our_device(candidate.port):
                    # Update session-cached port in place so downstream
                    # fixtures pick up the new path.
                    device_info.port = candidate.port
                    return candidate.port
        return None

    return _wait


def pytest_collection_modifyitems(config, items):
    """Auto-mark every test in this directory as ``hardware``.

    Saves callers from sprinkling ``pytestmark = pytest.mark.hardware``
    in every file. ``hardware_destructive`` and ``hardware_slow`` still
    need to be applied explicitly per test.

    Compares paths via ``Path.is_relative_to`` on 3.9+ and falls back
    to ``_HARDWARE_DIR in path.parents`` on 3.8 (the project's lowest
    supported Python; setup.py declares ``python_requires=">=3.8"``).
    """
    for item in items:
        try:
            item_path = Path(str(item.fspath)).resolve()
            try:
                is_under_hw = item_path.is_relative_to(_HARDWARE_DIR)
            except AttributeError:
                # Python 3.8 fallback — is_relative_to was added in 3.9.
                is_under_hw = _HARDWARE_DIR in item_path.parents
        except (ValueError, OSError):
            is_under_hw = False
        if is_under_hw:
            item.add_marker(pytest.mark.hardware)
