"""GitHub-backed concrete implementation of FirmwareDownloadService.

Ports the C# ``GitHubFirmwareDownloadService`` (sealed) from
``Daqifi.Core/Firmware/GitHubFirmwareDownloadService.cs`` @ upstream
e8d84af. Talks to the GitHub Releases REST API to discover firmware
releases, download the .hex assets, and (for the WiFi firmware repo)
download + extract a release zipball.

Implementation notes:
- The existing :class:`FirmwareDownloadService` Protocol is async (see
  ``daqifi/firmware/download_service.py``); this class matches that
  surface. Network I/O happens via :mod:`urllib.request` wrapped in
  :func:`asyncio.to_thread` so we don't pull in ``aiohttp`` /
  ``httpx`` as dependencies.
- Per-repository LRU cache with a TTL (defaults to 60 minutes,
  matching upstream).
- Zip-Slip protection on extracted entries (mirrors upstream's
  full-path StartsWith check).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .download_service import (
    FirmwareDownloadService,
    FirmwareUpdateCheckResult,
    ProgressReporter,
)
from .release_info import FirmwareReleaseInfo
from .version import FirmwareVersion


__all__ = ["GitHubFirmwareDownloadService"]


_DEFAULT_USER_AGENT = "DaqifiFirmwareUpdater/1.0"
_DEFAULT_FIRMWARE_REPO = "daqifi/daqifi-nyquist-firmware"
_DEFAULT_WIFI_REPO = "daqifi/winc1500-Manual-UART-Firmware-Update"
_DEFAULT_CACHE_TTL_SECONDS = 60 * 60  # 60 minutes
_DOWNLOAD_BUFFER_SIZE = 8192


@dataclass
class _Cache:
    releases: Optional[List[dict]] = None
    timestamp: float = 0.0


class GitHubFirmwareDownloadService(FirmwareDownloadService):
    """Fetches firmware releases from GitHub.

    Mirrors the C# sealed class. Each instance caches release
    enumerations per-repo with a TTL; call :meth:`invalidate_cache` to
    drop the cache (e.g. after publishing a new release).

    Construct with::

        svc = GitHubFirmwareDownloadService()
        info = await svc.get_latest_release()

    Custom repos / TTL via the constructor kwargs.
    """

    def __init__(
        self,
        firmware_repo: str = _DEFAULT_FIRMWARE_REPO,
        wifi_repo: str = _DEFAULT_WIFI_REPO,
        cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        self._firmware_api_url = (
            f"https://api.github.com/repos/{firmware_repo}/releases"
        )
        self._wifi_api_url = (
            f"https://api.github.com/repos/{wifi_repo}/releases"
        )
        self._cache_ttl = max(0, int(cache_ttl_seconds))
        self._user_agent = user_agent
        self._firmware_cache = _Cache()
        self._wifi_cache = _Cache()
        # Per-cache locks so two concurrent callers waiting on a cache
        # miss don't both fire the GitHub API request — risks
        # rate-limit pressure. The second caller awaits the first's
        # in-flight fetch and uses its result (Qodo PR 17 pass 4 #3).
        self._firmware_cache_lock = asyncio.Lock()
        self._wifi_cache_lock = asyncio.Lock()

    # ---- public Protocol surface ------------------------------------------

    async def get_latest_release(
        self,
        include_pre_release: bool = False,
    ) -> Optional[FirmwareReleaseInfo]:
        releases = await self._get_firmware_releases()
        return _find_latest_release(
            releases, include_pre_release, asset_extension=".hex"
        )

    async def check_for_update(
        self,
        device_version_string: str,
        include_pre_release: bool = False,
    ) -> FirmwareUpdateCheckResult:
        device_version = FirmwareVersion.parse(device_version_string)
        latest = await self.get_latest_release(include_pre_release)
        if latest is None:
            return FirmwareUpdateCheckResult(
                update_available=False,
                device_version=device_version,
                latest_release=None,
            )
        if device_version is None:
            update_available = True
        else:
            update_available = latest.version > device_version
        return FirmwareUpdateCheckResult(
            update_available=update_available,
            device_version=device_version,
            latest_release=latest,
        )

    async def download_latest_firmware(
        self,
        destination_directory: Path,
        include_pre_release: bool = False,
        progress: Optional[ProgressReporter] = None,
    ) -> Optional[Path]:
        release = await self.get_latest_release(include_pre_release)
        if release is None or release.download_url is None or release.asset_file_name is None:
            return None
        return await self._download_file(
            release.download_url,
            Path(destination_directory),
            release.asset_file_name,
            release.asset_size,
            progress,
        )

    async def download_firmware_by_tag(
        self,
        tag_name: str,
        destination_directory: Path,
        progress: Optional[ProgressReporter] = None,
    ) -> Optional[Path]:
        releases = await self._get_firmware_releases()
        release: Optional[FirmwareReleaseInfo] = None
        for element in releases:
            if element.get("draft"):
                continue
            tag = (element.get("tag_name") or "").strip()
            if tag.lower() != tag_name.lower():
                continue
            release = _parse_release_element(element, asset_extension=".hex")
            break
        if release is None or release.download_url is None or release.asset_file_name is None:
            return None
        return await self._download_file(
            release.download_url,
            Path(destination_directory),
            release.asset_file_name,
            release.asset_size,
            progress,
        )

    async def get_latest_wifi_release(self) -> Optional[FirmwareReleaseInfo]:
        releases = await self._get_wifi_releases()
        return _find_latest_release(releases, include_pre_release=False, asset_extension=None)

    async def download_wifi_firmware(
        self,
        destination_directory: Path,
        progress: Optional[ProgressReporter] = None,
    ) -> Optional[Tuple[Path, str]]:
        release = await self.get_latest_wifi_release()
        if release is None or release.zipball_url is None:
            return None

        dest = Path(destination_directory)
        dest.mkdir(parents=True, exist_ok=True)
        zip_path = dest / f"wifi-firmware-{release.tag_name}.zip"
        extract_path = dest / f"wifi-firmware-{release.tag_name}"

        _report(progress, 0)
        await asyncio.to_thread(
            _download_to_path_with_progress,
            release.zipball_url, zip_path, None, progress,
            self._user_agent,
            # Cap progress at 80% during download so extraction has room.
            80,
        )
        _report(progress, 85)

        # Replace any existing extraction directory (mirrors upstream).
        if extract_path.exists():
            await asyncio.to_thread(shutil.rmtree, extract_path)
        await asyncio.to_thread(_extract_zip_safely, zip_path, extract_path)

        # Clean up the downloaded zip file.
        await asyncio.to_thread(zip_path.unlink)
        _report(progress, 100)

        return (extract_path, release.tag_name)

    def invalidate_cache(self) -> None:
        self._firmware_cache = _Cache()
        self._wifi_cache = _Cache()

    # ---- internal helpers -------------------------------------------------

    async def _get_firmware_releases(self) -> List[dict]:
        return await self._get_releases_cached(
            self._firmware_cache, self._firmware_api_url, self._firmware_cache_lock,
        )

    async def _get_wifi_releases(self) -> List[dict]:
        return await self._get_releases_cached(
            self._wifi_cache, self._wifi_api_url, self._wifi_cache_lock,
        )

    async def _get_releases_cached(
        self, cache: _Cache, api_url: str, lock: asyncio.Lock,
    ) -> List[dict]:
        # Fast path: cache fresh — no lock needed.
        if cache.releases is not None and (time.time() - cache.timestamp) < self._cache_ttl:
            return cache.releases
        # Cache miss: serialize concurrent refreshers so only one
        # actually hits the GitHub API. Re-check inside the lock to
        # cover the case where a peer just finished refreshing.
        async with lock:
            if cache.releases is not None and (time.time() - cache.timestamp) < self._cache_ttl:
                return cache.releases
            releases = await asyncio.to_thread(
                _fetch_releases_from_api, api_url, self._user_agent
            )
            cache.releases = releases
            cache.timestamp = time.time()
            return releases

    async def _download_file(
        self,
        url: str,
        destination_directory: Path,
        file_name: str,
        expected_size: Optional[int],
        progress: Optional[ProgressReporter],
    ) -> Path:
        # Sanitize file name to prevent path traversal.
        safe_name = os.path.basename(file_name)
        if not safe_name:
            raise ValueError("Invalid file name.")
        destination_directory.mkdir(parents=True, exist_ok=True)
        target = destination_directory / safe_name
        await asyncio.to_thread(
            _download_to_path_with_progress,
            url, target, expected_size, progress,
            self._user_agent,
            100,  # full 0-100% range for direct downloads
        )
        _report(progress, 100)
        return target


# ---- module-level helpers ----------------------------------------------------


def _report(progress: Optional[ProgressReporter], value: int) -> None:
    if progress is None:
        return
    try:
        progress(value)
    except Exception:
        pass


def _fetch_releases_from_api(api_url: str, user_agent: str) -> List[dict]:
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": user_agent, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            reset_header = exc.headers.get("X-RateLimit-Reset")
            if reset_header:
                try:
                    reset_time = datetime.fromtimestamp(
                        int(reset_header), tz=timezone.utc
                    )
                    raise RuntimeError(
                        f"GitHub API rate limit exceeded. Resets at "
                        f"{reset_time.strftime('%Y-%m-%d %H:%M:%S %Z')}."
                    ) from exc
                except (ValueError, OverflowError):
                    pass
        raise
    data = json.loads(body)
    if not isinstance(data, list):
        raise RuntimeError("Unexpected GitHub releases response shape.")
    return data


def _download_to_path_with_progress(
    url: str,
    target: Path,
    expected_size: Optional[int],
    progress: Optional[ProgressReporter],
    user_agent: str,
    scale_to_percent: int,
) -> None:
    """Stream ``url`` into ``target``, reporting [0..scale_to_percent] progress.

    Mirrors the upstream's chunked-write + percentage-callback loop.
    Verifies expected_size when supplied. Writes to a temporary path
    and atomically renames on success so a crash mid-download leaves
    no half-file at the user-visible location.
    """
    # Reject anything that isn't HTTPS to a known GitHub release host.
    # The release dictionaries come from GitHub's own API but are
    # untrusted at the URL level — a tampered cache or a future
    # GitHub redirect could otherwise point us at an arbitrary host
    # (Qodo PR 17 pass 12 #3 — SSRF defense for the firmware path).
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Refusing to download non-HTTPS URL: {url}")
    if parsed.hostname not in {
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }:
        raise ValueError(
            f"Refusing to download from unexpected host: {parsed.hostname}"
        )

    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    target.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: download to a sibling tmp file, rename on success.
    fd, tmp_path = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            content_length = response.headers.get("Content-Length")
            # Use explicit `is not None` so a legitimate zero-byte
            # asset (expected_size == 0) doesn't fall through to the
            # Content-Length fallback (Qodo PR 17 pass 4 #5).
            if expected_size is not None:
                total = expected_size
            else:
                total = int(content_length) if content_length else -1
            bytes_read = 0
            with tmp.open("wb") as fp:
                while True:
                    chunk = response.read(_DOWNLOAD_BUFFER_SIZE)
                    if not chunk:
                        break
                    fp.write(chunk)
                    bytes_read += len(chunk)
                    if total > 0:
                        pct = int(min(bytes_read / total, 1.0) * scale_to_percent)
                        _report(progress, pct)

        if expected_size is not None and bytes_read != expected_size:
            raise RuntimeError(
                f"Downloaded file size ({bytes_read} bytes) does not match "
                f"expected size ({expected_size} bytes)."
            )

        # Atomic move (Path.replace handles cross-device on POSIX).
        tmp.replace(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def _extract_zip_safely(zip_path: Path, extract_path: Path) -> None:
    """Extract ``zip_path`` to ``extract_path`` with Zip-Slip protection.

    Defense-in-depth: also reject Unix symlink entries. Python's
    ``zipfile.extractall`` ignores symlink attributes by default and
    extracts them as regular files containing the link target as
    text — so a malicious archive can't sneak past directly — but
    explicit rejection avoids surprises if Python ever changes that
    behavior (Qodo PR 17 pass 7 #6).
    """
    extract_path.mkdir(parents=True, exist_ok=True)
    full_target = extract_path.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            member = info.filename
            # Symlink check: Unix create_system + mode bits 0o120000.
            if info.create_system == 3:  # 3 = Unix
                file_mode = (info.external_attr >> 16) & 0o170000
                if file_mode == 0o120000:
                    shutil.rmtree(extract_path, ignore_errors=True)
                    raise RuntimeError(
                        f"Zip archive contains symlink entry: {member}"
                    )
            target_path = (extract_path / member).resolve()
            try:
                target_path.relative_to(full_target)
            except ValueError as exc:
                # Path escapes the destination — abort + clean up.
                shutil.rmtree(extract_path, ignore_errors=True)
                raise RuntimeError(
                    f"Zip archive contains path traversal entry: {member}"
                ) from exc
        zf.extractall(extract_path)


def _find_latest_release(
    releases: List[dict],
    include_pre_release: bool,
    asset_extension: Optional[str],
) -> Optional[FirmwareReleaseInfo]:
    best: Optional[FirmwareReleaseInfo] = None
    for element in releases:
        if element.get("draft"):
            continue
        is_prerelease = bool(element.get("prerelease"))
        if not include_pre_release and is_prerelease:
            continue
        release = _parse_release_element(element, asset_extension)
        if release is None:
            continue
        # Skip releases without a downloadable asset when an extension was requested.
        if asset_extension is not None and release.download_url is None:
            continue
        if best is None or release.version > best.version:
            best = release
    return best


def _parse_release_element(
    element: dict,
    asset_extension: Optional[str],
) -> Optional[FirmwareReleaseInfo]:
    tag_name = (element.get("tag_name") or "").strip()
    if not tag_name:
        return None
    version = FirmwareVersion.parse(tag_name)
    if version is None:
        return None
    is_prerelease = bool(element.get("prerelease"))
    release_notes = element.get("body")
    published_at = _parse_iso_datetime(element.get("published_at"))

    download_url: Optional[str] = None
    asset_file_name: Optional[str] = None
    asset_size: Optional[int] = None

    if asset_extension is not None:
        for asset in (element.get("assets") or []):
            name = asset.get("name")
            if name and name.lower().endswith(asset_extension.lower()):
                download_url = asset.get("browser_download_url")
                asset_file_name = name
                size = asset.get("size")
                if isinstance(size, int):
                    asset_size = size
                break

    return FirmwareReleaseInfo(
        version=version,
        tag_name=tag_name,
        is_pre_release=is_prerelease,
        release_notes=release_notes,
        download_url=download_url,
        asset_file_name=asset_file_name,
        asset_size=asset_size,
        published_at=published_at,
        zipball_url=element.get("zipball_url"),
    )


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # GitHub returns RFC-3339 / ISO-8601 with trailing 'Z'.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None
