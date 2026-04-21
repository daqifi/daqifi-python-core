"""WiFi LAN chip info — JSON parsing helpers.

Port of `Daqifi.Core.Firmware.LanChipInfo` (record) and
`Daqifi.Core.Firmware.LanChipInfoParser` (static class). The C# parser
used `System.Text.Json`; we use `json` from the standard library.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional


__all__ = ["LanChipInfo", "try_parse", "try_parse_lines"]


@dataclass(frozen=True)
class LanChipInfo:
    """WiFi LAN chip info from `SYSTem:COMMunicate:LAN:GETChipInfo?`."""

    chip_id: int
    fw_version: str
    build_date: str


def try_parse(text: Optional[str]) -> Optional[LanChipInfo]:
    """Parse one JSON response into a `LanChipInfo`.

    Returns `None` if the input is missing, blank, malformed, or
    lacks a usable `FwVersion` field. Mirrors the C# `bool TryParse`
    contract: failure is silent, not raised.
    """
    if text is None or not text.strip():
        return None

    try:
        root = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return None

    if not isinstance(root, dict):
        return None

    fw_version = root.get("FwVersion")
    if not isinstance(fw_version, str) or not fw_version:
        return None

    chip_id_raw = root.get("ChipId", 0)
    chip_id = chip_id_raw if isinstance(chip_id_raw, int) and not isinstance(chip_id_raw, bool) else 0

    build_date_raw = root.get("BuildDate", "")
    build_date = build_date_raw if isinstance(build_date_raw, str) else ""

    return LanChipInfo(chip_id=chip_id, fw_version=fw_version, build_date=build_date)


def try_parse_lines(lines: Iterable[str]) -> Optional[LanChipInfo]:
    """Try each non-blank line in turn; return the first successful parse."""
    for line in lines:
        if not line or not line.strip():
            continue
        result = try_parse(line)
        if result is not None:
            return result
    return None
