"""#205 item 4: the hardware-free run must not reach for a board.

The hardware-free command (CONTRIBUTING.md, CI) is
``pytest tests --ignore=tests/integration/hardware`` (CI adds
``-m "not hardware"``). Every file under that path is marked ``hardware`` by
its conftest, so the path implies the marker. This keeps the other direction
true: no test outside the path carries a hardware marker, or asks for a
fixture from ``tests/integration/conftest.py`` that discovers a board, since
``DeviceFinder.discover_first()`` writes to every serial port. On a shared
bench that talks to boards other people hold.
"""

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_HARDWARE = _TESTS / "integration" / "hardware"
_SUITE = _TESTS / "integration" / "suite"  # collect_ignore_glob = ["*.py"]

# Fixtures in tests/integration/conftest.py that discover or open a board.
_DEVICE_FIXTURES = {
    "device_info", "device", "serial_transport", "error_drainer",
    "clean_device", "firmware_stats_diff", "wire_capture",
}


def _collected_test_modules():
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path.is_relative_to(_HARDWARE) or path.is_relative_to(_SUITE):
            continue
        yield path


def _hardware_marks(tree):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("hardware")
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        ):
            yield node.lineno


def _device_fixture_uses(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.arg in _DEVICE_FIXTURES:
                    yield node.lineno, node.name, arg.arg


def test_no_test_outside_the_hardware_path_needs_a_board() -> None:
    problems = []
    for path in _collected_test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_TESTS.parent)
        for line in _hardware_marks(tree):
            problems.append(f"{rel}:{line}: hardware marker outside tests/integration/hardware")
        # The board fixtures are visible only under tests/integration/;
        # a local fixture of the same name elsewhere is unrelated.
        if path.is_relative_to(_TESTS / "integration"):
            for line, func, arg in _device_fixture_uses(tree):
                problems.append(f"{rel}:{line}: {func}() uses the board fixture {arg!r}")
    assert not problems, "\n".join(problems)
