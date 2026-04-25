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


_HARDWARE_DIR = str(Path(__file__).resolve().parent)


def pytest_collection_modifyitems(config, items):
    """Auto-mark every test in this directory as ``hardware``.

    Saves callers from sprinkling ``pytestmark = pytest.mark.hardware``
    in every file. ``hardware_destructive`` and ``hardware_slow`` still
    need to be applied explicitly per test.
    """
    for item in items:
        if str(item.fspath).startswith(_HARDWARE_DIR):
            item.add_marker(pytest.mark.hardware)
