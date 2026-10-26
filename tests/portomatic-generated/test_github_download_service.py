"""Tests for GitHubFirmwareDownloadService port (daqifi/firmware/
github_download_service.py @ upstream e8d84af).

Mocks urllib at the function-level so the suite never touches the
network. Real-network smoke tests would live under tests/integration/."""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from daqifi.firmware.github_download_service import (
    GitHubFirmwareDownloadService,
    _find_latest_release,
    _parse_release_element,
)
from daqifi.firmware.release_info import FirmwareReleaseInfo


pytestmark = pytest.mark.portomatic_generated


# ---- _parse_release_element + _find_latest_release ---------------------------


def _release_dict(
    *,
    tag,
    pre=False,
    draft=False,
    assets=None,
    zipball=None,
    body="release notes",
    published="2026-04-22T10:00:00Z",
):
    return {
        "tag_name": tag,
        "prerelease": pre,
        "draft": draft,
        "body": body,
        "published_at": published,
        "assets": assets or [],
        "zipball_url": zipball,
    }


class TestParseReleaseElement:
    def test_basic_parse_with_hex_asset(self):
        elem = _release_dict(
            tag="v1.2.3",
            assets=[{
                "name": "firmware-1.2.3.hex",
                "browser_download_url": "https://example/firmware.hex",
                "size": 1024,
            }],
            zipball="https://example/zip",
        )
        info = _parse_release_element(elem, ".hex")
        assert info is not None
        assert info.tag_name == "v1.2.3"
        assert info.download_url == "https://example/firmware.hex"
        assert info.asset_file_name == "firmware-1.2.3.hex"
        assert info.asset_size == 1024
        assert info.zipball_url == "https://example/zip"
        assert info.is_pre_release is False
        assert info.release_notes == "release notes"
        assert info.published_at is not None

    def test_no_matching_asset_returns_none_url(self):
        elem = _release_dict(
            tag="v1.0.0",
            assets=[{
                "name": "source.zip",
                "browser_download_url": "https://example/src.zip",
                "size": 100,
            }],
        )
        info = _parse_release_element(elem, ".hex")
        assert info is not None
        # Asset extension didn't match — download fields stay None.
        assert info.download_url is None
        assert info.asset_file_name is None

    def test_unparseable_tag_returns_none(self):
        elem = _release_dict(tag="not-a-version")
        assert _parse_release_element(elem, ".hex") is None

    def test_empty_tag_returns_none(self):
        elem = _release_dict(tag="")
        assert _parse_release_element(elem, ".hex") is None

    def test_case_insensitive_asset_extension(self):
        elem = _release_dict(
            tag="v1.0.0",
            assets=[{
                "name": "FIRMWARE.HEX",
                "browser_download_url": "https://example/x",
                "size": 1,
            }],
        )
        info = _parse_release_element(elem, ".hex")
        assert info is not None
        assert info.download_url == "https://example/x"


class TestFindLatestRelease:
    def _make(self, tag, pre=False, draft=False, with_asset=True):
        assets = (
            [{"name": "fw.hex", "browser_download_url": "u", "size": 1}]
            if with_asset else []
        )
        return _release_dict(tag=tag, pre=pre, draft=draft, assets=assets)

    def test_returns_highest_version(self):
        elements = [
            self._make("v1.0.0"),
            self._make("v2.5.0"),
            self._make("v1.5.0"),
        ]
        latest = _find_latest_release(elements, include_pre_release=False, asset_extension=".hex")
        assert latest is not None
        assert latest.tag_name == "v2.5.0"

    def test_skips_drafts(self):
        elements = [self._make("v3.0.0", draft=True), self._make("v1.0.0")]
        latest = _find_latest_release(elements, include_pre_release=False, asset_extension=".hex")
        assert latest is not None
        assert latest.tag_name == "v1.0.0"

    def test_skips_prereleases_by_default(self):
        elements = [self._make("v3.0.0", pre=True), self._make("v1.0.0")]
        latest = _find_latest_release(elements, include_pre_release=False, asset_extension=".hex")
        assert latest is not None
        assert latest.tag_name == "v1.0.0"

    def test_includes_prereleases_when_requested(self):
        elements = [self._make("v3.0.0", pre=True), self._make("v1.0.0")]
        latest = _find_latest_release(elements, include_pre_release=True, asset_extension=".hex")
        assert latest is not None
        assert latest.tag_name == "v3.0.0"

    def test_skips_releases_without_matching_asset(self):
        elements = [self._make("v3.0.0", with_asset=False), self._make("v1.0.0")]
        latest = _find_latest_release(elements, include_pre_release=False, asset_extension=".hex")
        assert latest is not None
        assert latest.tag_name == "v1.0.0"

    def test_returns_none_when_empty(self):
        assert _find_latest_release([], False, ".hex") is None

    def test_no_extension_skip_check(self):
        # Wifi releases come through with asset_extension=None — must not
        # filter on download_url presence.
        elements = [self._make("v3.0.0", with_asset=False)]
        latest = _find_latest_release(elements, include_pre_release=False, asset_extension=None)
        assert latest is not None
        assert latest.tag_name == "v3.0.0"


# ---- async fetch + cache + check_for_update ---------------------------------


def _fake_fetch_factory(payload):
    def _fake(api_url, user_agent):
        return payload
    return _fake


_RELEASES_FIXTURE = [
    {
        "tag_name": "v2.0.0",
        "prerelease": False,
        "draft": False,
        "body": "release",
        "published_at": "2026-04-20T00:00:00Z",
        "assets": [{
            "name": "firmware-2.0.0.hex",
            "browser_download_url": "https://example/v2.hex",
            "size": 2048,
        }],
        "zipball_url": "https://example/v2.zip",
    },
    {
        "tag_name": "v1.0.0",
        "prerelease": False,
        "draft": False,
        "body": "old",
        "published_at": "2025-01-01T00:00:00Z",
        "assets": [{
            "name": "firmware-1.0.0.hex",
            "browser_download_url": "https://example/v1.hex",
            "size": 1024,
        }],
        "zipball_url": "https://example/v1.zip",
    },
]


class TestServiceAsyncSurface:
    def test_get_latest_release_returns_highest(self):
        svc = GitHubFirmwareDownloadService()
        with mock.patch(
            "daqifi.firmware.github_download_service._fetch_releases_from_api",
            _fake_fetch_factory(_RELEASES_FIXTURE),
        ):
            latest = asyncio.run(svc.get_latest_release())
        assert latest is not None
        assert latest.tag_name == "v2.0.0"
        assert latest.download_url == "https://example/v2.hex"

    def test_check_for_update_reports_when_newer_available(self):
        svc = GitHubFirmwareDownloadService()
        with mock.patch(
            "daqifi.firmware.github_download_service._fetch_releases_from_api",
            _fake_fetch_factory(_RELEASES_FIXTURE),
        ):
            result = asyncio.run(svc.check_for_update("1.0.0"))
        assert result.update_available is True
        assert result.latest_release is not None
        assert result.latest_release.tag_name == "v2.0.0"

    def test_check_for_update_when_already_latest(self):
        svc = GitHubFirmwareDownloadService()
        with mock.patch(
            "daqifi.firmware.github_download_service._fetch_releases_from_api",
            _fake_fetch_factory(_RELEASES_FIXTURE),
        ):
            result = asyncio.run(svc.check_for_update("2.0.0"))
        assert result.update_available is False

    def test_check_for_update_with_unparseable_device_version(self):
        svc = GitHubFirmwareDownloadService()
        with mock.patch(
            "daqifi.firmware.github_download_service._fetch_releases_from_api",
            _fake_fetch_factory(_RELEASES_FIXTURE),
        ):
            result = asyncio.run(svc.check_for_update("not-a-version"))
        # Unparseable device version → treat as out-of-date.
        assert result.update_available is True
        assert result.device_version is None

    def test_cache_avoids_second_fetch(self):
        svc = GitHubFirmwareDownloadService()
        calls = []

        def counting_fetch(api_url, user_agent):
            calls.append(api_url)
            return _RELEASES_FIXTURE

        with mock.patch(
            "daqifi.firmware.github_download_service._fetch_releases_from_api",
            counting_fetch,
        ):
            asyncio.run(svc.get_latest_release())
            asyncio.run(svc.get_latest_release())
        assert len(calls) == 1  # second call hit the cache

    def test_invalidate_cache_forces_refetch(self):
        svc = GitHubFirmwareDownloadService()
        calls = []

        def counting_fetch(api_url, user_agent):
            calls.append(api_url)
            return _RELEASES_FIXTURE

        with mock.patch(
            "daqifi.firmware.github_download_service._fetch_releases_from_api",
            counting_fetch,
        ):
            asyncio.run(svc.get_latest_release())
            svc.invalidate_cache()
            asyncio.run(svc.get_latest_release())
        assert len(calls) == 2

    def test_concurrent_misses_work_across_event_loops(self):
        # One service, driven by two separate asyncio.run() calls, each with
        # two callers racing a cache miss so the cache lock is contended. An
        # asyncio.Lock binds to the loop it is first contended on (3.10+) or
        # created on (3.9), so a lock built once in __init__ raised in the
        # second loop -- and on 3.9 the constructor itself raised once a
        # previous asyncio.run() had cleared the current loop.
        asyncio.run(asyncio.sleep(0))
        svc = GitHubFirmwareDownloadService()
        calls = []

        def slow_fetch(api_url, user_agent):
            calls.append(api_url)
            time.sleep(0.05)
            return _RELEASES_FIXTURE

        async def two_racing_callers():
            return await asyncio.gather(
                svc.get_latest_release(), svc.get_latest_release()
            )

        with mock.patch(
            "daqifi.firmware.github_download_service._fetch_releases_from_api",
            slow_fetch,
        ):
            first = asyncio.run(two_racing_callers())
            svc.invalidate_cache()
            second = asyncio.run(two_racing_callers())

        assert len(calls) == 2  # one fetch per loop; the racer waited
        assert all(r is not None for r in first + second)

    def test_download_firmware_by_tag_returns_none_for_missing_tag(self):
        svc = GitHubFirmwareDownloadService()
        with mock.patch(
            "daqifi.firmware.github_download_service._fetch_releases_from_api",
            _fake_fetch_factory(_RELEASES_FIXTURE),
        ):
            result = asyncio.run(svc.download_firmware_by_tag(
                "v9.9.9", Path("/tmp/none"),
            ))
        assert result is None


# ---- Zip-Slip protection -----------------------------------------------------


class TestExtractZipSafely:
    def test_safe_zip_extracts(self, tmp_path):
        from daqifi.firmware.github_download_service import _extract_zip_safely

        zip_path = tmp_path / "good.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("inner/foo.txt", "hello")
        target = tmp_path / "out"
        _extract_zip_safely(zip_path, target)
        assert (target / "inner" / "foo.txt").read_text() == "hello"

    def test_zip_slip_detected_and_rejected(self, tmp_path):
        from daqifi.firmware.github_download_service import _extract_zip_safely

        zip_path = tmp_path / "bad.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../escape.txt", "evil")
        target = tmp_path / "out"
        with pytest.raises(RuntimeError, match="path traversal"):
            _extract_zip_safely(zip_path, target)
        # Cleanup happened — the partially-extracted dir should not contain
        # the escaped file at the parent.
        assert not (tmp_path / "escape.txt").exists()
