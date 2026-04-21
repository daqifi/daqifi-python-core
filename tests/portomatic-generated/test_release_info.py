"""Tests for `daqifi.firmware.release_info` (port of FirmwareReleaseInfo)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from daqifi.firmware import FirmwareReleaseInfo, FirmwareVersion


class TestFirmwareReleaseInfo:
    def test_minimum_required_fields(self):
        v = FirmwareVersion.parse("1.2.3")
        assert v is not None
        info = FirmwareReleaseInfo(version=v, tag_name="v1.2.3", is_pre_release=False)
        assert info.version == v
        assert info.tag_name == "v1.2.3"
        assert info.is_pre_release is False
        assert info.release_notes is None
        assert info.download_url is None

    def test_full_field_set(self):
        v = FirmwareVersion.parse("3.0.0beta1")
        assert v is not None
        published = datetime(2026, 4, 1, tzinfo=timezone.utc)
        info = FirmwareReleaseInfo(
            version=v,
            tag_name="v3.0.0-beta.1",
            is_pre_release=True,
            release_notes="Initial beta",
            download_url="https://example.com/firmware.hex",
            asset_file_name="firmware.hex",
            asset_size=12345,
            published_at=published,
            zipball_url="https://example.com/source.zip",
        )
        assert info.version.major == 3
        assert info.is_pre_release is True
        assert info.asset_file_name == "firmware.hex"
        assert info.asset_size == 12345
        assert info.published_at == published

    def test_is_frozen(self):
        v = FirmwareVersion.parse("1.0.0")
        info = FirmwareReleaseInfo(version=v, tag_name="v1", is_pre_release=False)
        with pytest.raises(Exception):
            info.tag_name = "changed"  # type: ignore[misc]

    def test_equality_value_based(self):
        v = FirmwareVersion.parse("1.0.0")
        a = FirmwareReleaseInfo(version=v, tag_name="v1", is_pre_release=False)
        b = FirmwareReleaseInfo(version=v, tag_name="v1", is_pre_release=False)
        assert a == b
