"""HID device enumeration backed by an injectable platform layer.

Ports the C# ``HidLibraryDeviceEnumerator`` from
``Daqifi.Core/Communication/Transport/HidLibraryDeviceEnumerator.cs``.
The upstream class delegates to ``IHidPlatform`` so callers can inject a
fake during tests; the Python port keeps the same shape with a
:class:`HidPlatform` Protocol and a default :class:`HidApiPlatform`
backed by the ``hid`` (hidapi) package.

Importing this module does NOT require ``hid``. Constructing the default
:class:`HidLibraryDeviceEnumerator` does -- a clear :class:`ImportError`
is raised when the platform layer can't load. Tests pass a custom
platform and skip the import entirely.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Protocol, Sequence, runtime_checkable

from .comm import HidDeviceEnumerator, HidDeviceInfo


@runtime_checkable
class HidPlatform(Protocol):
    """Adapter for the underlying HID enumeration library.

    Mirrors the C# ``IHidPlatform`` interface. Implementations return an
    iterable of :class:`HidDeviceInfo`; the enumerator filters and
    sorts them.
    """

    def enumerate_devices(self) -> Iterable[HidDeviceInfo]:
        ...


def _describe_filter(vendor_id: Optional[int], product_id: Optional[int]) -> str:
    if vendor_id is not None and product_id is not None:
        return f" for VID=0x{vendor_id:04X}, PID=0x{product_id:04X}"
    if vendor_id is not None:
        return f" for VID=0x{vendor_id:04X}"
    if product_id is not None:
        return f" for PID=0x{product_id:04X}"
    return ""


class HidApiPlatform:
    """Default :class:`HidPlatform` backed by the ``hid`` (hidapi) package.

    Lazily imports ``hid`` inside :meth:`enumerate_devices` so importing
    this module never requires the package -- only constructing the
    default :class:`HidLibraryDeviceEnumerator` and actually enumerating
    triggers the dependency check.
    """

    def enumerate_devices(self) -> Iterable[HidDeviceInfo]:
        try:
            import hid  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "HID enumeration requires the 'hid' package "
                "(`pip install hid`). Pass a custom HidPlatform to "
                "HidLibraryDeviceEnumerator to avoid this dependency."
            ) from exc

        for entry in hid.enumerate():
            path = entry.get("path") or b""
            if isinstance(path, bytes):
                path = path.decode("utf-8", errors="replace")
            if not path or not path.strip():
                # HidDeviceInfo enforces a non-empty device_path; skip
                # entries the OS exposed without a usable path.
                continue
            yield HidDeviceInfo(
                vendor_id=entry.get("vendor_id", 0),
                product_id=entry.get("product_id", 0),
                device_path=path,
                serial_number=entry.get("serial_number") or None,
                product_name=entry.get("product_string") or None,
            )


class HidLibraryDeviceEnumerator(HidDeviceEnumerator):
    """Default HID enumerator implementation.

    Ports the C# ``HidLibraryDeviceEnumerator`` (sealed class).
    Wraps a :class:`HidPlatform` and applies the same VID/PID filter +
    device-path ordering as upstream.

    The async upstream signature ``Task<IReadOnlyList<HidDeviceInfo>>``
    becomes a sync :meth:`enumerate` returning a ``List[HidDeviceInfo]``
    (per the project's no-asyncio convention).
    """

    def __init__(self, platform: Optional[HidPlatform] = None) -> None:
        self._platform = platform if platform is not None else HidApiPlatform()

    def enumerate(
        self,
        vendor_id: Optional[int] = None,
        product_id: Optional[int] = None,
    ) -> Sequence[HidDeviceInfo]:
        try:
            devices: List[HidDeviceInfo] = [
                d
                for d in self._platform.enumerate_devices()
                if (vendor_id is None or d.vendor_id == vendor_id)
                and (product_id is None or d.product_id == product_id)
            ]
            devices.sort(key=lambda d: d.device_path)
            return devices
        except Exception as exc:
            raise RuntimeError(
                f"Failed to enumerate HID devices"
                f"{_describe_filter(vendor_id, product_id)}."
            ) from exc
