"""Firmware release information.

Port of `Daqifi.Core.Firmware.FirmwareReleaseInfo`. Captures one
firmware release entry surfaced by an upstream catalog (typically a
GitHub release).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from daqifi.firmware.version import FirmwareVersion


__all__ = ["FirmwareReleaseInfo"]


@dataclass(frozen=True)
class FirmwareReleaseInfo:
    """Information about one firmware release available for download."""

    version: FirmwareVersion
    tag_name: str
    is_pre_release: bool
    release_notes: Optional[str] = None
    download_url: Optional[str] = None
    asset_file_name: Optional[str] = None
    asset_size: Optional[int] = None
    published_at: Optional[datetime] = None
    zipball_url: Optional[str] = None
