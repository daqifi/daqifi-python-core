"""Parsed device firmware version with semantic versioning + pre-release support.

Ports `Daqifi.Core.Firmware.FirmwareVersion` (a C# `readonly record struct`).

Pre-release ordering follows the C# precedence:
    alpha/a/pre/preview/dev (0) < beta/b (1) < rc/releasecandidate (2) < release (3)

Ported from daqifi-core via portomatic v0.13 prompt
(plan: 20260421T050001-port-firmwareversion-v13).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Optional, Union


# Compiled once. Mirrors the C# regex semantics, plus an extension for
# dot-separated SemVer pre-releases:
# - optional leading `v` or `V`
# - major (required); minor + patch (optional, default 0)
# - one of TWO optional pre-release suffix shapes:
#   * concatenated (legacy / C# parity): "b2", "rc1", "alpha"
#   * dash + label + optional dot-number (SemVer / GitHub tags):
#     "-beta.1", "-rc.2", "-alpha"
# Closes daqifi/daqifi-python-core#10 — the C# regex only accepted the
# concatenated form, but the Python ecosystem (PEP 440, semver.org,
# GitHub release tags) overwhelmingly uses the dash-and-dot form.
_VERSION_RE = re.compile(
    r"""
    ^\s*
    v?\s*
    (?P<maj>\d+)
    (?:\.(?P<min>\d+))?
    (?:\.(?P<pat>\d+))?
    (?:
        # Dash form — separator before the number is optional so both
        # ``3.0.0-beta.1`` (SemVer / GitHub tags) AND ``3.0.0-beta1``
        # (some firmware self-reports) parse to the same fields. Single
        # capture group via the ``\.?`` optional separator instead of
        # a second capture (Qodo PR #52 /improve pass 1 importance 6).
        -(?P<dash_label>[A-Za-z]+)(?:\.?(?P<dash_num>\d+))?
        |
        (?P<suffix>[A-Za-z]+\d*)
    )?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Pre-release label → precedence rank. Higher = newer.
# Release (no label) sorts highest. Mirrors C# `GetPrecedenceRank`.
_PRECEDENCE = {
    "alpha": 0, "a": 0, "pre": 0, "preview": 0, "dev": 0,
    "beta": 1, "b": 1,
    "rc": 2, "releasecandidate": 2,
}
_RELEASE_RANK = 3


@total_ordering
@dataclass(frozen=True)
class FirmwareVersion:
    """Parsed firmware version: major.minor.patch with optional pre-release suffix."""

    major: int
    minor: int = 0
    patch: int = 0
    pre_label: Optional[str] = None
    pre_number: int = 0

    @property
    def is_pre_release(self) -> bool:
        """True when this version has a pre-release label (alpha/beta/rc/etc.)."""
        return bool(self.pre_label)

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        if not self.is_pre_release:
            return core
        suffix = self.pre_label or ""
        if self.pre_number > 0:
            suffix += str(self.pre_number)
        return core + suffix

    def _sort_key(self) -> tuple:
        return (
            self.major,
            self.minor,
            self.patch,
            _precedence_rank(self.pre_label),
            self.pre_number,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, FirmwareVersion):
            return NotImplemented
        return self._sort_key() < other._sort_key()

    @classmethod
    def parse(cls, value: Optional[str]) -> Optional["FirmwareVersion"]:
        """Parse a version string. Returns None if `value` is missing or unparseable.

        Examples that parse:
            "v3.2.0", "3.2.0", "3.2", "3", "3.2.0b2", "v1.0.0rc1", "1.0.0alpha"
        """
        if not value or not value.strip():
            return None
        m = _VERSION_RE.match(value.strip())
        if m is None:
            return None
        major = int(m.group("maj"))
        minor = int(m.group("min")) if m.group("min") else 0
        patch = int(m.group("pat")) if m.group("pat") else 0
        label: Optional[str] = None
        pre_number = 0
        # Dash-form SemVer pre-release ("3.0.0-beta.1") wins when present
        # — the regex's two suffix branches are mutually exclusive, so
        # only one of dash_label / suffix can be non-None per match.
        dash_label = m.group("dash_label")
        if dash_label:
            label = dash_label
            dash_num = m.group("dash_num")
            if dash_num:
                pre_number = int(dash_num)
        else:
            suffix = m.group("suffix")
            if suffix:
                # Split alphabetic prefix from trailing digits (e.g. "b2" → "b", 2).
                i = 0
                while i < len(suffix) and suffix[i].isalpha():
                    i += 1
                label = suffix[:i] or None
                num_part = suffix[i:]
                if num_part:
                    try:
                        pre_number = int(num_part)
                    except ValueError:
                        pre_number = 0
        # Canonicalize label to lowercase so equality / hash agree with
        # ordering. _precedence_rank lowercases internally for sort
        # ordering, so without canonicalization here parse("3.0.0BETA")
        # and parse("3.0.0beta") would compare equal via compare() but
        # NOT via dataclass equality (which sees the raw label strings)
        # and would hash to different values. (Closes Qodo PR #52
        # review #1.) Operators reading the label back get the
        # canonical lowercase form.
        if label is not None:
            label = label.lower()
        return cls(
            major=major,
            minor=minor,
            patch=patch,
            pre_label=label,
            pre_number=pre_number,
        )


# ---------------------------------------------------------------------------
# FirmwareRevision — non-SemVer hyphenated form (e.g. Nyquist's "01-02")
# ---------------------------------------------------------------------------
# Some firmware images self-report a revision identifier rather than a
# semantic version: the Nyquist NQ1/NQ2 returns "01-02" from
# SYST:INFo? as `<group>-<id>`. The C# `FirmwareVersion` rejects this
# shape and there is no equivalent type upstream — we add one here
# rather than widen `FirmwareVersion`, keeping the SemVer parser
# faithful to daqifi-core. Closes #9.

_REVISION_RE = re.compile(r"^\s*(?P<group>\d+)-(?P<rev>\d+)\s*$")


@total_ordering
@dataclass(frozen=True)
class FirmwareRevision:
    """Hyphenated firmware revision: ``<group>-<id>`` (e.g. ``01-02``)."""

    group: int
    revision: int

    def __str__(self) -> str:
        return f"{self.group:02d}-{self.revision:02d}"

    def _sort_key(self) -> tuple:
        return (self.group, self.revision)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, FirmwareRevision):
            return NotImplemented
        return self._sort_key() < other._sort_key()

    @classmethod
    def parse(cls, value: Optional[str]) -> Optional["FirmwareRevision"]:
        """Parse a hyphenated revision string. Returns None on missing/bad input."""
        if not value or not value.strip():
            return None
        m = _REVISION_RE.match(value)
        if m is None:
            return None
        return cls(group=int(m.group("group")), revision=int(m.group("rev")))


ParsedFirmware = Union[FirmwareVersion, FirmwareRevision]


def parse_firmware_string(value: Optional[str]) -> Optional[ParsedFirmware]:
    """Parse either a SemVer firmware version or a hyphenated revision.

    Picks the right shape based on input — returns ``FirmwareRevision`` for
    ``<int>-<int>`` and ``FirmwareVersion`` for SemVer-shaped values. Returns
    ``None`` if neither parser accepts the string.
    """
    if not value or not value.strip():
        return None
    rev = FirmwareRevision.parse(value)
    if rev is not None:
        return rev
    return FirmwareVersion.parse(value)


def compare(left: Optional[str], right: Optional[str]) -> int:
    """Compare two firmware strings (SemVer or hyphenated revision).

    Returns negative if ``left`` is older, positive if newer, 0 if equal.

    Sort policy:
    * Unparseable strings sort BEFORE everything (mirrors C# behavior on
      ``TryParse → false``).
    * ``FirmwareRevision`` (hyphenated) sorts BEFORE ``FirmwareVersion``
      (SemVer) — the hyphenated shape is the legacy form on Nyquist
      images, and a release that has migrated to SemVer should compare
      strictly newer than any pre-migration revision.
    * Same-shape comparisons use the native ordering on the parsed type.
    """
    parsed_left = parse_firmware_string(left)
    parsed_right = parse_firmware_string(right)
    if parsed_left is None and parsed_right is None:
        return 0
    if parsed_left is None:
        return -1
    if parsed_right is None:
        return 1
    if type(parsed_left) is type(parsed_right):
        if parsed_left < parsed_right:
            return -1
        if parsed_left > parsed_right:
            return 1
        return 0
    return -1 if isinstance(parsed_left, FirmwareRevision) else 1


def _precedence_rank(label: Optional[str]) -> int:
    if not label:
        return _RELEASE_RANK
    return _PRECEDENCE.get(label.lower(), 0)
