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


def _modinfo_parameters(
    module: str,
) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """Return ``([(param_name, type_and_description), ...], err)``.

    ``err`` is None when modinfo ran cleanly (the parameter list may
    still be empty — that's a real "no params advertised" result).
    On failure ``err`` is a short reason string and the param list is
    empty — caller MUST distinguish "no parameters exposed" from
    "modinfo couldn't run" (Qodo PR #78 /agentic_review pass 1 bug #3:
    don't conflate the two)."""
    if shutil.which("modinfo") is None:
        return [], "modinfo binary not found in PATH"
    try:
        proc = subprocess.run(
            ["modinfo", module],
            capture_output=True, text=True, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        # modinfo writes failure reason to stderr (e.g. "ERROR:
        # Module foo not found"). Surface it so callers don't
        # confuse "module unknown to modinfo" with "module has no
        # parameters."
        reason = (proc.stderr or proc.stdout or "").strip().splitlines()
        return [], reason[0] if reason else f"modinfo exit={proc.returncode}"
    params: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.startswith("parm:"):
            continue
        rest = line[len("parm:"):].strip()
        # Format: "name:description (type)"
        name, _, desc = rest.partition(":")
        params.append((name.strip(), desc.strip()))
    return params, None


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
    """Return the largest bulk-IN/OUT MaxPacketSize for a /dev/ttyACMx.

    cdc_acm splits a device into a Comm interface (carries the
    notification interrupt endpoint) and a Data interface (carries
    the bulk IN + bulk OUT endpoints). Endpoints live under sibling
    USB-interface directories — a flat scan of
    ``/sys/class/tty/<dev>/device/ep_*`` only catches the Comm-
    interface notification endpoint and misreports the device's bulk
    capacity. Walk the parent USB-device dir and read
    ``bmAttributes`` to filter to bulk (== 0x02) endpoints (Qodo
    PR #78 /agentic_review pass 1 bug #2).

    Returns None when no bulk endpoint is found under any sibling
    interface."""
    name = Path(device_path).name
    base = Path(f"/sys/class/tty/{name}/device")
    if not base.is_dir():
        return None
    # The /sys/class/tty/<dev>/device path is the USB *interface*.
    # Its parent is the USB *device*, which holds all interfaces +
    # their endpoints.
    try:
        usb_device = base.resolve().parent
    except OSError:
        usb_device = base
    largest: Optional[int] = None
    for ep in usb_device.rglob("ep_*"):
        wmax = ep / "wMaxPacketSize"
        attrs = ep / "bmAttributes"
        if not wmax.is_file():
            continue
        # bmAttributes lower 2 bits encode transfer type: 00=control,
        # 01=isoc, 02=bulk, 03=interrupt. Skip non-bulk.
        if attrs.is_file():
            try:
                attr_val = int(attrs.read_text().strip(), 16) & 0x03
            except (OSError, ValueError):
                continue
            if attr_val != 0x02:
                continue
        try:
            value = int(wmax.read_text().strip(), 16)
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

    params_meta, modinfo_err = _modinfo_parameters("cdc_acm")
    if modinfo_err is not None:
        print(f"\nKernel-advertised parameters: UNKNOWN ({modinfo_err})")
        print("  → can't tell whether nr_urbs is tunable on this build; "
              "install kmod (`apt install kmod`) and retry, or check "
              "`modinfo cdc_acm` manually.")
    else:
        print(f"\nKernel-advertised parameters ({len(params_meta)}):")
        if not params_meta:
            print("  none — this kernel build does NOT expose cdc_acm "
                  "parameters as module options.")
            print("  → `nr_urbs` cannot be tuned via modprobe.d on this "
                  "kernel.")
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
            print(f"  {path} (no bulk endpoint found)")
        else:
            # Bulk-IN/OUT max packet — Full-Speed = 64, High-Speed = 512.
            print(f"  {path} — bulk max packet = {ep} bytes")

    nr_urbs = next(
        (v for n, v in runtime if n.lower() in {"nr_urbs", "nrurbs"}),
        None,
    )
    print("\n== Recommendation ==")
    if nr_urbs is None:
        if modinfo_err is not None:
            print(
                "Couldn't query module parameters via modinfo "
                f"({modinfo_err}); install kmod and retry to determine "
                "whether nr_urbs is tunable on this kernel."
            )
        elif not params_meta:
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
