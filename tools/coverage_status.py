#!/usr/bin/env python3
"""Print the portomatic coverage record for one or more symbol names.

Usage:
    tools/coverage_status.py IDevice ScpiMessageProducer
    tools/coverage_status.py --all-missing
    tools/coverage_status.py --suspicious  # only matches that look wrong

Output format (TSV-ish, one symbol per line):
    upstream_name  status  overlap  downstream_match  match_strategy

A match is "suspicious" when:
    - status is `ported` or `partial` AND overlap < 0.6, OR
    - the upstream and downstream names share only a single token, OR
    - the upstream kind and downstream-match kind disagree (e.g.
      interface matched to enum, class matched to free function).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
PORTOMATIC_BIN = (
    Path("/mnt/c/Users/User/Documents/GitHub/portomatic/.venv/bin/portomatic")
)


def _load_coverage() -> dict:
    if not PORTOMATIC_BIN.exists():
        sys.exit(f"portomatic binary not found at {PORTOMATIC_BIN}")
    proc = subprocess.run(
        [str(PORTOMATIC_BIN), "coverage", "--downstream", str(REPO_ROOT),
         "--detail", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def _tokens(name: str) -> set[str]:
    """Cheap PascalCase token split: ABDef -> {A, BDef} or AbcDef -> {Abc, Def}."""
    out: list[str] = []
    cur: list[str] = []
    for ch in name:
        if ch.isupper() and cur:
            out.append("".join(cur))
            cur = [ch]
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return {t.lower() for t in out if t}


def _is_suspicious(entry: dict) -> bool:
    if entry["status"] not in ("ported", "partial"):
        return False
    # Strategy-driven matches (e.g. module-of-functions) are intentional
    # — token overlap heuristics don't apply.
    if entry.get("match_strategy"):
        return False
    if entry.get("overlap", 0) < 0.6:
        return True
    match = entry.get("downstream_match") or ""
    if not match:
        return False
    # Compare tokens: e.g. IDevice vs DeviceType -> {device} vs {device, type}
    # = single overlap token, suspicious.
    upstream_name = entry.get("upstream_name", "")
    down_symbol = match.rsplit(":", 1)[-1] if ":" in match else match
    # Strip leading I-prefix that we drop in Python ports.
    if (
        upstream_name.startswith("I")
        and len(upstream_name) > 1
        and upstream_name[1].isupper()
    ):
        upstream_name = upstream_name[1:]
    up_t = _tokens(upstream_name)
    down_t = _tokens(down_symbol)
    overlap = up_t & down_t
    # Multi-vs-multi: at most one shared token = suspicious.
    if len(overlap) <= 1 and len(up_t) >= 2 and len(down_t) >= 2:
        return True
    # Single-vs-multi (e.g. `Device` vs `DeviceType`) -- the longer side
    # is doing all the discrimination, so an exact-1.0 overlap there
    # almost always means the classifier latched onto a substring.
    if len(overlap) == 1 and abs(len(up_t) - len(down_t)) >= 1:
        return True
    return False


def _print(entries: Iterable[dict]) -> None:
    for e in entries:
        print(
            "{name}\t{status}\t{overlap:.2f}\t{match}\t{strategy}".format(
                name=e.get("upstream_name", "?"),
                status=e.get("status", "?"),
                overlap=e.get("overlap", 0.0),
                match=e.get("downstream_match") or "-",
                strategy=e.get("match_strategy") or "-",
            )
        )


def main(argv: list[str]) -> int:
    if not argv or "--help" in argv or "-h" in argv:
        print(__doc__, file=sys.stderr)
        return 2

    data = _load_coverage()
    entries = data.get("entries", [])
    by_name = {e["upstream_name"]: e for e in entries}

    if argv[0] == "--all-missing":
        _print(e for e in entries if e["status"] == "missing")
        return 0
    if argv[0] == "--suspicious":
        _print(e for e in entries if _is_suspicious(e))
        return 0

    missing = []
    for name in argv:
        e = by_name.get(name)
        if e is None:
            missing.append(name)
            continue
        _print([e])
    if missing:
        print("not found in coverage report:", *missing, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
