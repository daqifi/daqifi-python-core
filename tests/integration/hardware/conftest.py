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

from pathlib import Path

import pytest


_HARDWARE_DIR = Path(__file__).resolve().parent


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
