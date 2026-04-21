"""SD card data types ported from `Daqifi.Core.Device.SdCard`.

Includes the small value classes the parser surface needs:
- `SdCardFileInfo` — name + optional creation date for one log file
- `SdCardParseProgress` — progress callback payload
- `SdCardDeviceConfiguration` — device snapshot for parsing context
- `SdCardParseOptions` — knobs the caller passes to a parse run

**Divergence from upstream:**
- `SdCardDeviceConfiguration.from_device()` is intentionally
  omitted — it depends on `DaqifiDevice`, `IAnalogChannel`,
  `ChannelType` (none of which are ported yet). Callers populate
  fields directly. Reintroduce as a separate port step once those
  upstream types land.
- `SdCardParseOptions.progress` is typed `Optional[Callable[...]]`
  rather than the C# `IProgress<T>` interface. Python idiom is a
  callable; introducing a `Protocol` would be ceremony with no win.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Tuple


__all__ = [
    "SdCardFileInfo",
    "SdCardParseProgress",
    "SdCardDeviceConfiguration",
    "SdCardParseOptions",
]


@dataclass(frozen=True)
class SdCardFileInfo:
    """One file on the device SD card.

    `created_date` is parsed from the filename when available; `None`
    if the parser couldn't extract it.
    """

    file_name: str
    created_date: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.file_name is None:
            raise TypeError("file_name must not be None")


@dataclass(frozen=True)
class SdCardParseProgress:
    """Progress information emitted during SD card parsing."""

    bytes_read: int
    total_bytes: int  # -1 when unknown
    messages_read: int


@dataclass(frozen=True)
class SdCardDeviceConfiguration:
    """Device snapshot used to fill in fields missing from the SD card file."""

    analog_port_count: int
    digital_port_count: int
    timestamp_frequency: int
    device_serial_number: Optional[str] = None
    device_part_number: Optional[str] = None
    firmware_revision: Optional[str] = None
    calibration_values: Optional[List[Tuple[float, float]]] = None  # (slope, intercept) per channel
    resolution: int = 0
    port_range: Optional[List[float]] = None
    internal_scale_m: Optional[List[float]] = None


@dataclass
class SdCardParseOptions:
    """Knobs controlling SD card log file parsing.

    Mutable on purpose — matches the C# property-setter shape and
    lets callers configure the object stepwise.
    """

    session_start_time: Optional[datetime] = None
    buffer_size: int = 64 * 1024
    progress: Optional[Callable[[SdCardParseProgress], None]] = None
    fallback_timestamp_frequency: int = 50_000_000
    configuration_override: Optional[SdCardDeviceConfiguration] = field(default=None)
