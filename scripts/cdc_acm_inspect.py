"""Inspect cdc_acm tuning state on this host (#18 §1).

Reports cdc_acm module presence, current NR_URBS / module parameters,
attached DAQiFi USB CDC endpoints + their MaxPacketSize, and whether
this is a WSL2 guest. Read-only — does not modify modprobe configs
or rebind the module. Used to decide whether the modprobe.d recipe
in ``docs/usb_cdc_tuning_linux.md`` would actually help.

Usage:
    python3 scripts/cdc_acm_inspect.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def _is_wsl2() -> bool:
    try:
        with open("/proc/sys/kernel/osrelease") as f:
            return "microsoft" in f.read().lower()
    except (FileNotFoundError, PermissionError):
        return False


def _module_loaded(name: str) -> bool:
    return Path(f"/sys/module/{name}").is_dir()


def _modinfo_parameters(module: str) -> List[Tuple[str, str]]:
    """Return ``[(param_name, type_and_description), ...]`` for the module.

    Falls back to empty when ``modinfo`` isn't available (e.g. inside
    minimal containers without kmod tools)."""
    if shutil.which("modinfo") is None:
        return []
    try:
        out = subprocess.run(
            ["modinfo", module],
            capture_output=True, text=True, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    params: List[Tuple[str, str]] = []
    for line in out.splitlines():
        if not line.startswith("parm:"):
            continue
        rest = line[len("parm:"):].strip()
        # Format: "name:description (type)"
        name, _, desc = rest.partition(":")
        params.append((name.strip(), desc.strip()))
    return params


def _runtime_module_params(module: str) -> List[Tuple[str, str]]:
    """Return current runtime parameter values from
    ``/sys/module/<m>/parameters/<param>``. Empty when the module
    exposes no writable parameters."""
    base = Path(f"/sys/module/{module}/parameters")
    if not base.is_dir():
        return []
    entries: List[Tuple[str, str]] = []
    for child in sorted(base.iterdir()):
        if not child.is_file():
            continue
        try:
            value = child.read_text().strip()
        except (PermissionError, OSError):
            value = "<unreadable>"
        entries.append((child.name, value))
    return entries


def _attached_cdc_acm_devices() -> List[str]:
    """Return the device paths bound to cdc_acm (e.g. ttyACM0)."""
    base = Path("/sys/class/tty")
    if not base.is_dir():
        return []
    bound: List[str] = []
    for child in sorted(base.iterdir()):
        if not child.name.startswith("ttyACM"):
            continue
        # Walk to its driver symlink and confirm it points at cdc_acm.
        driver = child / "device" / "driver"
        try:
            target = driver.resolve()
            if target.name == "cdc_acm":
                bound.append(f"/dev/{child.name}")
        except OSError:
            continue
    return bound


def _bulk_endpoint_max_packet(device_path: str) -> Optional[int]:
    """Return the largest bulk-endpoint MaxPacketSize for a /dev/ttyACMx.

    Walks ``/sys/class/tty/<dev>/device/../`` to find the USB
    interface and its endpoint descriptors. Returns None when the
    path doesn't expose a USB endpoint (rare, indicates a
    different driver bound to that ACM number)."""
    name = Path(device_path).name
    base = Path(f"/sys/class/tty/{name}/device")
    if not base.is_dir():
        return None
    largest: Optional[int] = None
    for ep in base.glob("ep_*"):
        wmax = ep / "wMaxPacketSize"
        if not wmax.is_file():
            continue
        try:
            text = wmax.read_text().strip()
            # Format on Linux: hex, no prefix, e.g. "0040" for 64.
            value = int(text, 16)
        except (OSError, ValueError):
            continue
        if largest is None or value > largest:
            largest = value
    return largest


def main() -> int:
    print("== cdc_acm tuning inspection ==\n")

    print(f"WSL2 guest: {_is_wsl2()}")
    if not _is_wsl2():
        try:
            with open("/proc/sys/kernel/osrelease") as f:
                print(f"Kernel: {f.read().strip()}")
        except OSError:
            pass

    loaded = _module_loaded("cdc_acm")
    print(f"\ncdc_acm module loaded: {loaded}")
    if not loaded:
        print("  (load with `sudo modprobe cdc_acm` and re-run)")
        return 0

    params_meta = _modinfo_parameters("cdc_acm")
    print(f"\nKernel-advertised parameters ({len(params_meta)}):")
    if not params_meta:
        print("  none — this kernel build does NOT expose cdc_acm "
              "parameters as module options.")
        print("  → `nr_urbs` cannot be tuned via modprobe.d on this kernel.")
        print("  → On WSL2 this is the common case; see "
              "docs/usb_cdc_tuning_linux.md for fallback options.")
    for name, desc in params_meta:
        print(f"  {name}: {desc}")

    runtime = _runtime_module_params("cdc_acm")
    print(f"\nCurrent runtime parameter values ({len(runtime)}):")
    for name, value in runtime:
        print(f"  {name} = {value}")

    bound = _attached_cdc_acm_devices()
    print(f"\nDevices currently bound to cdc_acm ({len(bound)}):")
    for path in bound:
        ep = _bulk_endpoint_max_packet(path)
        if ep is None:
            print(f"  {path} (endpoint info unavailable)")
        else:
            # Walk every endpoint, not just bulk — we report the
            # largest. Full-Speed bulk = 64, High-Speed bulk = 512;
            # interrupt endpoints are typically smaller (8/16). A
            # value below 64 likely means we walked the interrupt
            # endpoint and the bulk endpoint lives under a sibling
            # interface (cdc_acm splits comm + data).
            print(f"  {path} — max endpoint = {ep} bytes")

    nr_urbs = next(
        (v for n, v in runtime if n.lower() in {"nr_urbs", "nrurbs"}),
        None,
    )
    print("\n== Recommendation ==")
    if nr_urbs is None:
        if not params_meta:
            print(
                "Your kernel doesn't expose cdc_acm parameters at runtime — "
                "the modprobe.d recipe in docs/usb_cdc_tuning_linux.md will "
                "load harmlessly but won't change behavior. Common on WSL2."
            )
        else:
            print(
                "cdc_acm exposes parameters but not nr_urbs on this build — "
                "see modinfo output above for what IS tunable."
            )
    else:
        try:
            current = int(nr_urbs)
        except ValueError:
            current = -1
        if current >= 64:
            print(
                f"nr_urbs is already {nr_urbs} — at or above the "
                "recommended target. No action needed."
            )
        elif current >= 0:
            print(
                f"nr_urbs is currently {nr_urbs}. Recommended: 64 for "
                "high-rate USB-CDC streaming. See "
                "docs/usb_cdc_tuning_linux.md for the modprobe.d recipe."
            )
        else:
            print(
                f"nr_urbs reads as {nr_urbs!r} — couldn't parse as int. "
                "Inspect manually."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
