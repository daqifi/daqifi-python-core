"""Tests for `daqifi.sdcard` (port of SD card value types)."""
from __future__ import annotations

from datetime import datetime

import pytest

from daqifi.sdcard import (
    SdCardDeviceConfiguration,
    SdCardFileInfo,
    SdCardParseOptions,
    SdCardParseProgress,
)


class TestSdCardFileInfo:
    def test_construct_with_name_only(self):
        info = SdCardFileInfo(file_name="20240101.bin")
        assert info.file_name == "20240101.bin"
        assert info.created_date is None

    def test_construct_with_date(self):
        d = datetime(2024, 1, 1, 12, 0, 0)
        info = SdCardFileInfo(file_name="log.bin", created_date=d)
        assert info.created_date == d

    def test_none_filename_raises(self):
        with pytest.raises(TypeError):
            SdCardFileInfo(file_name=None)  # type: ignore[arg-type]

    def test_is_frozen(self):
        info = SdCardFileInfo(file_name="x")
        with pytest.raises(Exception):
            info.file_name = "y"  # type: ignore[misc]


class TestSdCardParseProgress:
    def test_construct_sets_fields(self):
        p = SdCardParseProgress(bytes_read=100, total_bytes=1000, messages_read=5)
        assert p.bytes_read == 100
        assert p.total_bytes == 1000
        assert p.messages_read == 5

    def test_unknown_total_uses_negative_one(self):
        p = SdCardParseProgress(bytes_read=42, total_bytes=-1, messages_read=2)
        assert p.total_bytes == -1

    def test_is_frozen(self):
        p = SdCardParseProgress(bytes_read=0, total_bytes=0, messages_read=0)
        with pytest.raises(Exception):
            p.bytes_read = 1  # type: ignore[misc]


class TestSdCardDeviceConfiguration:
    def test_minimal_required_fields(self):
        cfg = SdCardDeviceConfiguration(
            analog_port_count=4,
            digital_port_count=8,
            timestamp_frequency=50_000_000,
        )
        assert cfg.analog_port_count == 4
        assert cfg.digital_port_count == 8
        assert cfg.timestamp_frequency == 50_000_000
        assert cfg.device_serial_number is None
        assert cfg.calibration_values is None
        assert cfg.resolution == 0

    def test_full_field_set(self):
        cfg = SdCardDeviceConfiguration(
            analog_port_count=2,
            digital_port_count=4,
            timestamp_frequency=50_000_000,
            device_serial_number="SN001",
            device_part_number="PN-X",
            firmware_revision="1.2.3",
            calibration_values=[(1.0, 0.0), (1.5, -0.1)],
            resolution=65535,
            port_range=[10.0, 5.0],
            internal_scale_m=[1.0, 1.0],
        )
        assert cfg.device_serial_number == "SN001"
        assert cfg.calibration_values == [(1.0, 0.0), (1.5, -0.1)]

    def test_is_frozen(self):
        cfg = SdCardDeviceConfiguration(0, 0, 0)
        with pytest.raises(Exception):
            cfg.resolution = 1  # type: ignore[misc]


class TestSdCardParseOptions:
    def test_defaults(self):
        opts = SdCardParseOptions()
        assert opts.session_start_time is None
        assert opts.buffer_size == 64 * 1024
        assert opts.progress is None
        assert opts.fallback_timestamp_frequency == 50_000_000
        assert opts.configuration_override is None

    def test_progress_is_callable_slot(self):
        calls = []
        opts = SdCardParseOptions(progress=lambda p: calls.append(p))
        # Simulate calling it.
        progress = SdCardParseProgress(1, 2, 3)
        opts.progress(progress)  # type: ignore[misc]
        assert calls == [progress]

    def test_mutable(self):
        opts = SdCardParseOptions()
        opts.buffer_size = 1024
        assert opts.buffer_size == 1024

    def test_zero_fallback_disables(self):
        opts = SdCardParseOptions(fallback_timestamp_frequency=0)
        assert opts.fallback_timestamp_frequency == 0

    def test_with_configuration_override(self):
        cfg = SdCardDeviceConfiguration(1, 2, 50_000_000)
        opts = SdCardParseOptions(configuration_override=cfg)
        assert opts.configuration_override is cfg
