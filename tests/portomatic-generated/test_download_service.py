"""Tests for `daqifi.firmware.download_service`.

The Protocol's value type (`FirmwareUpdateCheckResult`), and the download
paths of its implementation, `GitHubFirmwareDownloadService`, run for real:
only the release lookup and the HTTP response are faked, so the streaming,
size check, host allowlist, redirect check and zip extraction are the
library's own (#205 item 1; this file used to test a fake of its own).
"""
from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from daqifi.firmware import (
    FirmwareDownloadService,
    FirmwareReleaseInfo,
    FirmwareUpdateCheckResult,
    FirmwareVersion,
)
from daqifi.firmware.github_download_service import GitHubFirmwareDownloadService


class TestFirmwareUpdateCheckResult:
    def test_minimum_required_field(self):
        r = FirmwareUpdateCheckResult(update_available=False)
        assert r.update_available is False
        assert r.device_version is None
        assert r.latest_release is None

    def test_full_fields(self):
        v = FirmwareVersion.parse("1.2.3")
        release = FirmwareReleaseInfo(version=v, tag_name="v1.2.3", is_pre_release=False)
        r = FirmwareUpdateCheckResult(
            update_available=True,
            device_version=v,
            latest_release=release,
        )
        assert r.update_available is True
        assert r.device_version == v
        assert r.latest_release == release

    def test_is_frozen(self):
        r = FirmwareUpdateCheckResult(update_available=False)
        with pytest.raises(Exception):
            r.update_available = True  # type: ignore[misc]


_HEX = b":10000000" + bytes(range(256)) * 12  # 3 KiB, several read chunks
_HEX_URL = "https://github.com/daqifi/daqifi-nyquist-firmware/releases/download/v2.0.0/firmware-2.0.0.hex"
_OLD_HEX = b":old firmware"
_OLD_HEX_URL = "https://github.com/daqifi/daqifi-nyquist-firmware/releases/download/v1.0.0/firmware-1.0.0.hex"
_WIFI_ZIP_URL = "https://api.github.com/repos/daqifi/winc1500-Manual-UART-Firmware-Update/zipball/v19.5.4"


def _wifi_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("winc/firmware/m2m_aio_3a0.bin", b"winc image")
        z.writestr("winc/download_all.bat", b"@echo off")
    return buf.getvalue()


def _release(tag, *, asset=None, size=None, zipball=None):
    assets = []
    if asset is not None:
        assets.append({
            "name": asset.rsplit("/", 1)[1],
            "browser_download_url": asset,
            "size": size,
        })
    return {
        "tag_name": tag, "prerelease": False, "draft": False, "body": "",
        "published_at": "2026-04-20T00:00:00Z", "assets": assets,
        "zipball_url": zipball,
    }


def _releases(api_url, user_agent, hex_size=len(_HEX), hex_url=_HEX_URL):
    if "winc1500" in api_url:
        return [_release("v19.5.4", zipball=_WIFI_ZIP_URL)]
    return [
        _release("v2.0.0", asset=hex_url, size=hex_size),
        _release("v1.0.0", asset=_OLD_HEX_URL, size=len(_OLD_HEX)),
    ]


class _Response:
    def __init__(self, body: bytes, final_url: str, content_length: bool = True):
        self._body = io.BytesIO(body)
        self._final_url = final_url
        self.headers = {"Content-Length": str(len(body))} if content_length else {}

    def geturl(self):
        return self._final_url

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(bodies, redirect_to=None, content_length=True):
    """A urlopen that answers each URL in ``bodies``; records the URLs."""
    calls = []

    def _urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        return _Response(bodies[url], redirect_to or url, content_length)

    return _urlopen, calls


def _run(coro, releases=_releases, urlopen=None):
    patches = [mock.patch(
        "daqifi.firmware.github_download_service._fetch_releases_from_api", releases
    )]
    if urlopen is not None:
        patches.append(mock.patch(
            "daqifi.firmware.github_download_service.urllib.request.urlopen", urlopen
        ))
    with patches[0]:
        if len(patches) == 1:
            return asyncio.run(coro)
        with patches[1]:
            return asyncio.run(coro)


class TestGitHubDownloadService:
    def test_it_satisfies_the_protocol(self):
        assert isinstance(GitHubFirmwareDownloadService(), FirmwareDownloadService)

    def test_download_latest_firmware_writes_the_asset_and_reports_progress(self, tmp_path):
        urlopen, calls = _serve({_HEX_URL: _HEX})
        progress: list = []
        path = _run(
            GitHubFirmwareDownloadService().download_latest_firmware(tmp_path, progress=progress.append),
            urlopen=urlopen,
        )
        assert path == tmp_path / "firmware-2.0.0.hex"
        assert path.read_bytes() == _HEX
        assert calls == [_HEX_URL]
        assert progress and progress[-1] == 100
        assert progress == sorted(progress)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["firmware-2.0.0.hex"]

    def test_a_download_of_unknown_size_still_reports_completion(self, tmp_path):
        # No asset size and no Content-Length: no per-chunk percentages,
        # but the caller still hears 100 when the file is there.
        urlopen, _ = _serve({_HEX_URL: _HEX}, content_length=False)
        releases = lambda url, ua: _releases(url, ua, hex_size=None)
        progress: list = []
        path = _run(
            GitHubFirmwareDownloadService().download_latest_firmware(tmp_path, progress=progress.append),
            releases=releases, urlopen=urlopen,
        )
        assert path.read_bytes() == _HEX
        assert progress == [100]

    def test_a_short_download_raises_and_leaves_no_file(self, tmp_path):
        urlopen, _ = _serve({_HEX_URL: _HEX[:1000]})
        with pytest.raises(RuntimeError, match="does not match"):
            _run(GitHubFirmwareDownloadService().download_latest_firmware(tmp_path), urlopen=urlopen)
        assert list(tmp_path.iterdir()) == []

    def test_an_asset_off_github_is_refused_before_any_request(self, tmp_path):
        urlopen, calls = _serve({})
        releases = lambda url, ua: _releases(url, ua, hex_url="https://example.com/firmware-2.0.0.hex")
        with pytest.raises(ValueError, match="unexpected host"):
            _run(GitHubFirmwareDownloadService().download_latest_firmware(tmp_path),
                 releases=releases, urlopen=urlopen)
        assert calls == []
        assert list(tmp_path.iterdir()) == []

    def test_a_redirect_off_github_is_refused(self, tmp_path):
        urlopen, _ = _serve({_HEX_URL: _HEX}, redirect_to="https://evil.example/firmware.hex")
        with pytest.raises(ValueError, match="redirected"):
            _run(GitHubFirmwareDownloadService().download_latest_firmware(tmp_path), urlopen=urlopen)
        assert list(tmp_path.iterdir()) == []

    def test_download_by_tag_fetches_that_release(self, tmp_path):
        urlopen, calls = _serve({_OLD_HEX_URL: _OLD_HEX})
        path = _run(
            GitHubFirmwareDownloadService().download_firmware_by_tag("V1.0.0", tmp_path),
            urlopen=urlopen,
        )
        assert path == tmp_path / "firmware-1.0.0.hex"
        assert path.read_bytes() == _OLD_HEX
        assert calls == [_OLD_HEX_URL]

    def test_an_unknown_tag_downloads_nothing(self, tmp_path):
        urlopen, calls = _serve({})
        assert _run(
            GitHubFirmwareDownloadService().download_firmware_by_tag("v9.9.9", tmp_path),
            urlopen=urlopen,
        ) is None
        assert calls == []

    def test_download_wifi_firmware_extracts_the_zipball(self, tmp_path):
        urlopen, calls = _serve({_WIFI_ZIP_URL: _wifi_zip()})
        progress: list = []
        result = _run(
            GitHubFirmwareDownloadService().download_wifi_firmware(tmp_path, progress=progress.append),
            urlopen=urlopen,
        )
        assert result is not None
        path, tag = result
        assert tag == "v19.5.4"
        assert path == tmp_path / "wifi-firmware-v19.5.4"
        assert (path / "winc/firmware/m2m_aio_3a0.bin").read_bytes() == b"winc image"
        assert (path / "winc/download_all.bat").read_bytes() == b"@echo off"
        # The zip is removed once extracted; only the tree remains.
        assert [p.name for p in tmp_path.iterdir()] == ["wifi-firmware-v19.5.4"]
        assert calls == [_WIFI_ZIP_URL]
        assert progress[0] == 0 and 85 in progress and progress[-1] == 100
