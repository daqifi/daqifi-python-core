"""Hardware-test collection guard.

Every ``test_*.py`` in this directory:

* Is automatically marked ``@pytest.mark.hardware`` (via ``pytestmark``
  set on collected items below) — so ``pytest -m hardware`` selects them
  and ``pytest -m "not hardware"`` excludes them.
* Skip-collects when no DAQiFi device is available (the parent
  ``tests/integration/conftest.py`` already does this via the
  session-scoped ``device_info`` fixture; we just need each test to
  depend on a fixture that depends on ``device_info``).

The ``streaming_device``, ``clean_device``, ``error_drainer``,
``firmware_stats_diff``, ``wire_capture`` and ``destructive_ok``
fixtures live one directory up — see
``tests/integration/conftest.py``.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-mark every test in this directory as ``hardware``.

    Saves callers from sprinkling ``pytestmark = pytest.mark.hardware``
    in every file. ``hardware_destructive`` and ``hardware_slow`` still
    need to be applied explicitly per test.
    """
    here = str(__import__("pathlib").Path(__file__).resolve().parent)
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(pytest.mark.hardware)
