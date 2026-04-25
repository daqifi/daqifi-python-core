"""Runtime payloads emitted by the firmware update lifecycle.

Ports three closely related types from upstream Daqifi.Core/Firmware:

- :class:`FirmwareUpdateProgress` — periodic progress payload.
- :class:`FirmwareUpdateStateChangedEventArgs` — state-transition event
  data (mirrors C# ``EventArgs``-derived class).
- :class:`FirmwareUpdateException` — error type carrying state +
  operation context.

All three live in one module because they're consumed together by the
update service and are too small to warrant separate files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from daqifi.enums import FirmwareUpdateState


@dataclass
class FirmwareUpdateProgress:
    """Progress payload emitted by firmware update operations.

    Mirrors the mutable C# ``FirmwareUpdateProgress`` class — the update
    service mutates ``percent_complete`` / ``current_operation`` /
    ``bytes_written`` between emissions, so this dataclass is intentionally
    left mutable (no ``frozen=True``).
    """

    state: FirmwareUpdateState = FirmwareUpdateState.IDLE
    percent_complete: float = 0.0  # 0.0-100.0
    current_operation: str = ""
    bytes_written: int = 0
    total_bytes: int = 0


@dataclass(frozen=True)
class FirmwareUpdateStateChangedEventArgs:
    """Event data describing a firmware update state transition.

    Frozen because the upstream type exposes only ``get;`` properties
    (immutable once constructed). ``changed_at_utc`` defaults to the
    current UTC instant when the event is created, matching the
    upstream constructor's ``DateTimeOffset.UtcNow`` initialization.
    """

    previous_state: FirmwareUpdateState
    current_state: FirmwareUpdateState
    operation: str = ""
    changed_at_utc: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class FirmwareUpdateException(Exception):
    """Firmware update failure with state + operation context.

    Ports the C# ``FirmwareUpdateException`` (derives from ``Exception``).
    Adds ``failed_state``, ``operation``, and ``recovery_guidance``
    attributes so callers can surface actionable diagnostics.
    """

    def __init__(
        self,
        failed_state: FirmwareUpdateState,
        operation: str,
        message: str,
        recovery_guidance: Optional[str] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.failed_state = failed_state
        self.operation = operation or ""
        self.recovery_guidance = recovery_guidance
        if cause is not None:
            self.__cause__ = cause
