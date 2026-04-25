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
from datetime import datetime, timedelta
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from daqifi.enums import SdCardLogFormat


__all__ = [
    "SdCardFileInfo",
    "SdCardParseProgress",
    "SdCardDeviceConfiguration",
    "SdCardParseOptions",
    "SdCardTransferProgress",
    "SdCardDownloadResult",
    "SdCardOperations",
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
        if not self.file_name.strip():
            raise ValueError("file_name must not be empty or whitespace")


@dataclass(frozen=True)
class SdCardParseProgress:
    """Progress information emitted during SD card parsing."""

    bytes_read: int
    total_bytes: int  # -1 when unknown
    messages_read: int


@dataclass(frozen=True)
class SdCardDeviceConfiguration:
    """Device snapshot used to fill in fields missing from the SD card file.

    Container fields use ``Tuple`` rather than ``List`` so the
    "frozen" dataclass guarantee actually holds — a mutable list
    inside a frozen dataclass is a footgun (Qodo PR 17 pass 10 #6).
    Callers may pass any iterable; ``__post_init__`` coerces to
    tuples for safety.
    """

    analog_port_count: int
    digital_port_count: int
    timestamp_frequency: int
    device_serial_number: Optional[str] = None
    device_part_number: Optional[str] = None
    firmware_revision: Optional[str] = None
    # (slope, intercept) per channel
    calibration_values: Optional[Tuple[Tuple[float, float], ...]] = None
    resolution: int = 0
    port_range: Optional[Tuple[float, ...]] = None
    internal_scale_m: Optional[Tuple[float, ...]] = None

    def __post_init__(self) -> None:
        # frozen=True bans normal attribute assignment, so fall
        # through object.__setattr__ to coerce iterables → tuples.
        if self.calibration_values is not None:
            coerced = tuple(tuple(p) for p in self.calibration_values)
            # Each entry must be a (slope, intercept) pair —
            # downstream code unpacks two values (Qodo PR 17 pass 18 #2).
            if any(len(pair) != 2 for pair in coerced):
                raise ValueError(
                    "calibration_values entries must be "
                    "(slope, intercept) 2-tuples"
                )
            object.__setattr__(self, "calibration_values", coerced)
        if self.port_range is not None:
            object.__setattr__(self, "port_range", tuple(self.port_range))
        if self.internal_scale_m is not None:
            object.__setattr__(self, "internal_scale_m", tuple(self.internal_scale_m))


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


@dataclass(frozen=True)
class SdCardTransferProgress:
    """Progress payload reported during an SD card file download.

    Ports the C# ``sealed record SdCardTransferProgress`` from
    ``Daqifi.Core/Device/SdCard/SdCardTransferProgress.cs``. Frozen
    because the upstream record is immutable; the download path emits
    a fresh instance per progress tick rather than mutating one.
    """

    bytes_received: int
    file_name: str


@dataclass(frozen=True)
class SdCardDownloadResult:
    """Metadata returned at the end of an SD card file download.

    Ports the C# ``sealed record SdCardDownloadResult`` from
    ``Daqifi.Core/Device/SdCard/SdCardDownloadResult.cs``. ``file_path``
    is ``None`` when the file was streamed to a caller-provided buffer
    (no on-disk artifact); set when the convenience overload writes to
    a temporary file.
    """

    file_name: str
    file_size: int
    duration: timedelta
    file_path: Optional[str] = None


@runtime_checkable
class SdCardOperations(Protocol):
    """Protocol for SD-card operations on a DAQiFi device.

    Pythonic name for the C# ``ISdCardOperations`` interface from
    ``Daqifi.Core/Device/SdCard/ISdCardOperations.cs`` (the ``I``
    prefix is dropped to match the rest of this codebase's protocols).

    Async methods mirror the C# ``Task`` / ``Task<T>`` returns. The
    download convenience overload from C# (no destination stream) is
    surfaced as :meth:`download_sd_card_file_to_temp` so the two
    overloads have distinct names — Python doesn't dispatch on
    parameter types like C# does.
    """

    @property
    def is_logging_to_sd_card(self) -> bool:
        """True while the device is actively logging to its SD card."""
        ...

    @property
    def sd_card_files(self) -> Sequence[SdCardFileInfo]:
        """Most recently retrieved file listing.

        Read-only sequence — implementations should return a tuple or
        an immutable view rather than a live list.
        """
        ...

    async def get_sd_card_files(self) -> Sequence[SdCardFileInfo]:
        """Fetch and return the current list of files on the SD card.

        Raises:
            RuntimeError: if the device is not connected.
        """
        ...

    async def start_sd_card_logging(
        self,
        file_name: Optional[str] = None,
        channel_mask: Optional[str] = None,
        format: SdCardLogFormat = SdCardLogFormat.PROTOBUF,
    ) -> None:
        """Begin logging to the SD card.

        Args:
            file_name: Output file name. If empty/``None``, the device
                generates a timestamped name like ``log_YYYYMMDD_HHMMSS``
                with an extension matching ``format`` (``.bin`` for
                Protobuf, ``.json`` for JSON, ``.csv`` for CSV).
            channel_mask: Optional decimal bitmask string enabling
                specific ADC channels (each bit = one channel). When
                empty/``None`` the current device channel configuration
                is used.
            format: Logging format — protobuf, JSON, or CSV.

        Raises:
            RuntimeError: if the device is not connected.
        """
        ...

    async def stop_sd_card_logging(self) -> None:
        """Stop the active SD card logging session.

        Raises:
            RuntimeError: if the device is not connected.
        """
        ...

    async def delete_sd_card_file(self, file_name: str) -> None:
        """Delete ``file_name`` from the SD card.

        Raises:
            RuntimeError: if the device is not connected or is currently
                logging to the SD card.
            ValueError: if ``file_name`` is empty or contains invalid
                characters.
        """
        ...

    async def format_sd_card(self) -> None:
        """Erase the entire SD card.

        Raises:
            RuntimeError: if the device is not connected or is currently
                logging.
        """
        ...

    async def download_sd_card_file(
        self,
        file_name: str,
        destination_stream: Any,
        progress: Optional[Callable[[SdCardTransferProgress], None]] = None,
    ) -> SdCardDownloadResult:
        """Stream a file from the device's SD card into ``destination_stream``.

        ``destination_stream`` is any binary-writable file-like object
        (anything supporting ``.write(bytes) -> int``) — keeps the
        contract duck-typed rather than tying it to ``BinaryIO``.
        ``progress``, if provided, is called with one
        :class:`SdCardTransferProgress` per chunk.

        Raises:
            RuntimeError: if the device is not connected or is not
                using a USB/serial transport.
            ValueError: if ``file_name`` is empty or invalid.
        """
        ...

    async def download_sd_card_file_to_temp(
        self,
        file_name: str,
        progress: Optional[Callable[[SdCardTransferProgress], None]] = None,
    ) -> SdCardDownloadResult:
        """Download a file to a temporary on-disk location.

        Convenience overload of :meth:`download_sd_card_file` that
        manages the destination file itself; the resulting
        :class:`SdCardDownloadResult` carries the local path in
        ``file_path``. Renamed from the C# overload because Python
        can't dispatch on parameter types.

        Raises:
            RuntimeError: if the device is not connected or is not
                using a USB/serial transport.
            ValueError: if ``file_name`` is empty or invalid.
        """
        ...
