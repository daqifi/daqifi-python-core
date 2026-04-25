"""Hardware-coverage gate.

Walks every public symbol in ``daqifi.*`` (top-level modules + the
``daqifi.firmware`` subpackage) and confirms each one is mentioned in
``tests/integration/hardware/COVERAGE.md``. A symbol is "mentioned" if
its bare name (e.g. ``SerialStreamTransport.connect`` or
``DeviceFinder.get_serial_ports``) appears in a checkbox row of the
coverage doc.

Exit codes:
    0 — every public symbol is listed
    1 — one or more public symbols are missing from COVERAGE.md
    2 — bad invocation / can't import daqifi

Usage::

    python scripts/check_hardware_coverage.py            # report only
    python scripts/check_hardware_coverage.py --strict   # exit non-zero
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
import sys
from pathlib import Path
from typing import Iterable, Set

# Modules whose public symbols we care about. ``daqifi._internal`` is
# implementation detail — skip.
TOP_MODULES = (
    "daqifi.transport",
    "daqifi.communication",
    "daqifi.device",
    "daqifi.devices",
    "daqifi.device_finders",
    "daqifi.discovery",
    "daqifi.channels",
    "daqifi.streaming",
    "daqifi.sdcard",
    "daqifi.network",
    "daqifi.protocol",
    "daqifi.timestamp",
    "daqifi.exceptions",
    "daqifi.enums",
    "daqifi.version",
)
FIRMWARE_PACKAGE = "daqifi.firmware"

COVERAGE_DOC = (
    Path(__file__).resolve().parent.parent
    / "tests" / "integration" / "hardware" / "COVERAGE.md"
)


def _is_public_name(name: str) -> bool:
    return not name.startswith("_")


def _public_members(module) -> Iterable[tuple[str, object]]:
    for name, value in inspect.getmembers(module):
        if not _is_public_name(name):
            continue
        # Only emit symbols defined in this module (skip re-exports).
        try:
            origin = inspect.getmodule(value)
        except Exception:
            origin = None
        if origin is not None and origin is not module:
            continue
        yield name, value


# Members inherited from Python builtins that we never want to test.
_INHERITED_NOISE = {
    "add_note", "with_traceback", "args",  # BaseException
    "mro", "register",  # ABCMeta
}


def collect_public_symbols() -> Set[str]:
    """Return the set of fully-qualified public symbol names to check.

    Format:
        ``ClassName`` (top-level class)
        ``ClassName.method_name`` (instance method, public class members)
        ``function_name`` (module-level function)
    """
    symbols: Set[str] = set()

    def _scan(module) -> None:
        for name, value in _public_members(module):
            if inspect.isclass(value):
                symbols.add(name)
                # Only enumerate members defined ON this class (not inherited).
                # Without this universal guard, inherited @property descriptors
                # on non-Exception classes would also get added — the previous
                # version only skipped inherited members for Exception subclasses,
                # so e.g. `AnalogInputs.channels` inherited from a generic
                # `ChannelCollection` would be falsely listed as needing its own
                # coverage entry.
                own_attrs = set(value.__dict__.keys())
                for member_name, member in inspect.getmembers(value):
                    if not _is_public_name(member_name):
                        continue
                    if member_name in _INHERITED_NOISE:
                        continue
                    if member_name not in own_attrs:
                        continue
                    # Unwrap staticmethod / classmethod descriptors so they
                    # match inspect.isfunction below.
                    raw = value.__dict__.get(member_name, member)
                    if isinstance(raw, (staticmethod, classmethod)):
                        raw = raw.__func__
                    if inspect.isfunction(raw) or isinstance(member, property):
                        symbols.add(f"{name}.{member_name}")
                    elif callable(member) and not member_name.startswith("__"):
                        symbols.add(f"{name}.{member_name}")
            elif inspect.isfunction(value):
                symbols.add(name)
            # Module-level constants / dataclasses without methods land
            # via the class branch above — leave plain values alone.

    import_failures: list[str] = []

    for mod_name in TOP_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:
            msg = f"could not import {mod_name}: {exc}"
            print(f"WARN: {msg}", file=sys.stderr)
            import_failures.append(msg)
            continue
        _scan(mod)

    # Also scan the firmware subpackage's submodules.
    try:
        fw_pkg = importlib.import_module(FIRMWARE_PACKAGE)
    except Exception as exc:
        msg = f"could not import {FIRMWARE_PACKAGE}: {exc}"
        print(f"WARN: {msg}", file=sys.stderr)
        import_failures.append(msg)
    else:
        for _, sub_name, _ispkg in pkgutil.iter_modules(fw_pkg.__path__):
            if sub_name.startswith("_"):
                continue
            try:
                sub = importlib.import_module(f"{FIRMWARE_PACKAGE}.{sub_name}")
            except Exception as exc:
                msg = f"could not import {FIRMWARE_PACKAGE}.{sub_name}: {exc}"
                print(f"WARN: {msg}", file=sys.stderr)
                import_failures.append(msg)
                continue
            _scan(sub)

    if not symbols:
        print(
            "ERROR: no symbols collected; daqifi imports likely failed entirely",
            file=sys.stderr,
        )
        if import_failures:
            print("Import failures:", file=sys.stderr)
            for f in import_failures:
                print(f"  - {f}", file=sys.stderr)
        sys.exit(2)

    return symbols


def parse_coverage_doc(path: Path) -> Set[str]:
    r"""Return the set of symbol names mentioned in checkbox rows.

    Supports the doc's shorthand: a row like
    ``- [ ] `NyquistDevice.variant` / `manufacturer` / `model` (B9)``
    counts ``NyquistDevice.variant``, ``NyquistDevice.manufacturer``,
    and ``NyquistDevice.model`` as listed (the bare names after the
    first dotted name inherit the same class prefix until the next
    dotted name appears).
    """
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(2)
    text = path.read_text(encoding="utf-8")
    listed: Set[str] = set()
    row_pattern = re.compile(r"^\s*-\s*\[[ x~ds]\]\s+(.*)$", re.MULTILINE)
    name_pattern = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")
    for row_match in row_pattern.finditer(text):
        row_body = row_match.group(1)
        current_prefix: str | None = None
        for name_match in name_pattern.finditer(row_body):
            token = name_match.group(1)
            listed.add(token)
            if "." in token:
                current_prefix = token.split(".", 1)[0]
                listed.add(current_prefix)
            elif current_prefix is not None:
                # Bare name inherits the prefix from the row.
                listed.add(f"{current_prefix}.{token}")
    return listed


def main() -> int:
    strict = "--strict" in sys.argv

    needed = collect_public_symbols()
    listed = parse_coverage_doc(COVERAGE_DOC)

    missing = sorted(s for s in needed if s not in listed)

    print(f"Public symbols discovered: {len(needed)}")
    print(f"Symbols listed in COVERAGE.md: {len(listed)}")
    print(f"Missing from COVERAGE.md: {len(missing)}")
    if missing:
        print("\nMissing symbols:")
        for s in missing:
            print(f"  - {s}")
        if strict:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
