"""Opt this directory out of pytest collection.

These files are runnable scripts mirrored from the upstream
``daqifi-python-test-suite`` (see README.md). Several of them open
the serial port at module top level, which would cause pytest's
collection phase to hang or hard-fail on any machine without the
device attached. They are invoked directly (``python3 test_<x>.py``),
not via pytest.
"""
collect_ignore_glob = ["*.py"]
