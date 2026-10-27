"""The HID code needs the PyPI ``hidapi`` package, whose module is ``hid``.

The unrelated PyPI ``hid`` package installs a module of the same name with a
different API (``hid.Device``); the old error message told users to install it.
"""

import sys
import types

import pytest

from daqifi._internal.comm import HidDeviceInfo
from daqifi._internal.hid_enumerator import HidApiPlatform, import_hidapi
from daqifi.transport import _HidConnectedDevice


def _fake_hid(monkeypatch, **attributes):
    module = types.ModuleType("hid")
    for name, value in attributes.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "hid", module)
    return module


def test_missing_module_names_the_hidapi_package(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "hid", None)  # import hid -> ImportError
    with pytest.raises(ImportError) as exc_info:
        import_hidapi("HID transport", "")
    message = str(exc_info.value)
    assert "pip install hidapi" in message
    assert "pip install hid`" not in message


def test_the_incompatible_hid_package_is_recognised(monkeypatch) -> None:
    # PyPI "hid" 1.0.9: hid.Device and hid.enumerate, no hid.device.
    _fake_hid(monkeypatch, Device=object, enumerate=lambda *a: [])
    with pytest.raises(ImportError, match="is not it.*pip install hidapi"):
        import_hidapi("HID transport", "")


def test_hidapi_module_is_returned(monkeypatch) -> None:
    module = _fake_hid(monkeypatch, device=lambda: object(), enumerate=lambda *a: [])
    assert import_hidapi("HID transport", "") is module


def test_transport_and_enumeration_both_refuse_the_wrong_package(monkeypatch) -> None:
    _fake_hid(monkeypatch, Device=object, enumerate=lambda *a: [])
    info = HidDeviceInfo(0x04D8, 0x003C, "TEST-HID-PATH", None, "Bootloader")
    with pytest.raises(ImportError, match="hidapi"):
        _HidConnectedDevice.open_default(info)
    with pytest.raises(ImportError, match="hidapi"):
        list(HidApiPlatform().enumerate_devices())
