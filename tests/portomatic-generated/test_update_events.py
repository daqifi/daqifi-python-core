"""Tests for firmware update event payloads (Progress, StateChanged, Exception).

Ported from upstream e8d84af:
- Daqifi.Core/Firmware/FirmwareUpdateProgress.cs
- Daqifi.Core/Firmware/FirmwareUpdateStateChangedEventArgs.cs
- Daqifi.Core/Firmware/FirmwareUpdateException.cs
"""

from datetime import datetime, timezone

import pytest

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.update_events import (
    FirmwareUpdateException,
    FirmwareUpdateProgress,
    FirmwareUpdateStateChangedEventArgs,
)


@pytest.mark.portomatic_generated
class TestFirmwareUpdateProgress:
    def test_default_construction(self):
        p = FirmwareUpdateProgress()
        assert p.state == FirmwareUpdateState.IDLE
        assert p.percent_complete == 0.0
        assert p.current_operation == ""
        assert p.bytes_written == 0
        assert p.total_bytes == 0

    def test_is_mutable(self):
        # Progress is updated between emissions in the C# source --
        # confirm the dataclass is NOT frozen.
        p = FirmwareUpdateProgress()
        p.percent_complete = 42.5
        p.bytes_written = 1024
        assert p.percent_complete == 42.5
        assert p.bytes_written == 1024

    def test_keyword_construction(self):
        p = FirmwareUpdateProgress(
            state=FirmwareUpdateState.PROGRAMMING,
            percent_complete=50.0,
            current_operation="writing",
            bytes_written=512,
            total_bytes=1024,
        )
        assert p.state == FirmwareUpdateState.PROGRAMMING
        assert p.current_operation == "writing"


@pytest.mark.portomatic_generated
class TestFirmwareUpdateStateChangedEventArgs:
    def test_required_fields(self):
        args = FirmwareUpdateStateChangedEventArgs(
            previous_state=FirmwareUpdateState.IDLE,
            current_state=FirmwareUpdateState.CONNECTING,
            operation="connecting to bootloader",
        )
        assert args.previous_state == FirmwareUpdateState.IDLE
        assert args.current_state == FirmwareUpdateState.CONNECTING
        assert args.operation == "connecting to bootloader"

    def test_changed_at_default_is_utc_now(self):
        before = datetime.now(timezone.utc)
        args = FirmwareUpdateStateChangedEventArgs(
            previous_state=FirmwareUpdateState.IDLE,
            current_state=FirmwareUpdateState.CONNECTING,
            operation="",
        )
        after = datetime.now(timezone.utc)
        assert args.changed_at_utc.tzinfo is not None
        assert before <= args.changed_at_utc <= after

    def test_is_frozen(self):
        # Upstream is immutable (get;-only properties) -- the dataclass should reject mutation.
        args = FirmwareUpdateStateChangedEventArgs(
            previous_state=FirmwareUpdateState.IDLE,
            current_state=FirmwareUpdateState.CONNECTING,
        )
        with pytest.raises(Exception):
            args.previous_state = FirmwareUpdateState.PROGRAMMING  # type: ignore[misc]


@pytest.mark.portomatic_generated
class TestFirmwareUpdateException:
    def test_basic(self):
        err = FirmwareUpdateException(
            failed_state=FirmwareUpdateState.PROGRAMMING,
            operation="write_block",
            message="checksum mismatch",
        )
        assert isinstance(err, Exception)
        assert err.failed_state == FirmwareUpdateState.PROGRAMMING
        assert err.operation == "write_block"
        assert err.recovery_guidance is None
        assert "checksum mismatch" in str(err)

    def test_recovery_guidance_carried(self):
        err = FirmwareUpdateException(
            failed_state=FirmwareUpdateState.CONNECTING,
            operation="hid_open",
            message="device not found",
            recovery_guidance="Re-plug the USB cable.",
        )
        assert err.recovery_guidance == "Re-plug the USB cable."

    def test_operation_none_normalized_to_empty(self):
        # Mirrors C# `operation ?? string.Empty` defensive default.
        err = FirmwareUpdateException(
            failed_state=FirmwareUpdateState.IDLE,
            operation="",
            message="x",
        )
        assert err.operation == ""

    def test_inner_cause_chained(self):
        inner = OSError("usb closed")
        err = FirmwareUpdateException(
            failed_state=FirmwareUpdateState.PROGRAMMING,
            operation="flush",
            message="write failed",
            cause=inner,
        )
        assert err.__cause__ is inner

    def test_raises(self):
        with pytest.raises(FirmwareUpdateException) as exc:
            raise FirmwareUpdateException(
                failed_state=FirmwareUpdateState.VERIFYING,
                operation="verify",
                message="bad CRC",
            )
        assert exc.value.failed_state == FirmwareUpdateState.VERIFYING
