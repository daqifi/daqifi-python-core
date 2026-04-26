"""One-shot migration: pilot map vocabulary -> portomatic schema vocabulary.

Preserved in-tree for audit-trail value + reproducibility. The
migration ran in 2026-04 to retire the in-repo pilot scripts
(``scripts/portomatic_generate_map.py`` /
``scripts/portomatic_audit.py``) once ``portomatic map suggest`` /
``portomatic map audit`` shipped upstream.

Three idempotent passes (re-runs on already-migrated maps are no-ops):

1. **Vocabulary translation.** The pilot maps used a local relation
   set (``ported``/``missing``/``downstream-only``) that doesn't
   match the official ``portomatic.map.schema`` enum. Translates:

   - ``ported``           -> ``one_to_one``
   - ``missing``          -> ``not_yet_ported``
   - ``downstream-only``  -> ``divergent`` (with auto-rationale)
   - ``divergent``        -> ``divergent``  (no change)
   - ``dropped``          -> ``dropped``    (no change)

   Also translates ``SymbolRef.kind`` values not in the schema's
   enum (``dataclass``/``protocol``/``record`` -> ``class``/
   ``interface``/``class``), strips member-level extra fields
   (``upstream_kind``/``downstream_kind``/``rationale``), and
   bumps ``schema_version: 2 -> 1``.

2. **IR-based downstream-only recovery.** The pilot maps had
   downstream-only member rows with ``upstream: null``, which the
   schema's ``MemberMapping.upstream: str`` rejects. Pass 1 drops
   them; this pass walks the downstream IR and adds them back as
   ``divergent`` with a ``<downstream-only:{name}>`` placeholder
   upstream. Recovers public Python methods + properties.

3. **Git-history downstream-only recovery.** The Python adapter
   doesn't extract enum values + dataclass fields, so the IR-based
   pass misses those. Reads the pre-migration pilot maps from a
   configurable git ref + recovers the rest.

After running, all 12 shards validate against the official
``portomatic.map.schema.MapShard`` (verified via
``portomatic map validate --downstream .``).

Run::

    python scripts/migrate_pilot_maps.py --downstream-ir <ir.json>

The downstream IR is produced via::

    portomatic adapter extract --lang python --path daqifi --root . --out <ir.json>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_DIR = REPO_ROOT / ".portomatic" / "map"

# Last commit before the migration ran. Pass 3 reads pilot maps at
# this ref to recover downstream-only rows that the Python adapter
# can't surface (enum values + dataclass fields).
DEFAULT_PILOT_REF = "e663d0d"

_DOWNSTREAM_ONLY_RATIONALE = (
    "Downstream-only addition; no upstream counterpart."
)

_RELATION_TRANSLATION = {
    "ported": "one_to_one",
    "missing": "not_yet_ported",
    "divergent": "divergent",
    "dropped": "dropped",
}

_KIND_TRANSLATION = {
    "dataclass": "class",
    "protocol": "interface",
    "record": "class",
}


# ---- Pass 1 — vocabulary translation -------------------------------------


def _translate_relation(rel):
    if rel is None:
        return (None, None)
    if rel == "downstream-only":
        return ("divergent", _DOWNSTREAM_ONLY_RATIONALE)
    return (_RELATION_TRANSLATION.get(rel, rel), None)


def _migrate_member(m):
    if not isinstance(m, dict):
        return m
    new_rel, _auto = _translate_relation(m.get("relation"))
    upstream = m.get("upstream")
    if upstream is None:
        return None
    out = {
        "upstream": upstream,
        "downstream": m.get("downstream"),
        "relation": new_rel,
    }
    notes = m.get("notes") or m.get("rationale")
    if notes is not None:
        out["notes"] = notes
    if m.get("backlog_id") is not None:
        out["backlog_id"] = m["backlog_id"]
    return out


def _migrate_symbol_ref(ref):
    if not isinstance(ref, dict):
        return ref
    out = dict(ref)
    if out.get("kind") in _KIND_TRANSLATION:
        out["kind"] = _KIND_TRANSLATION[out["kind"]]
    return out


def _migrate_file_or_symbol_relation(d):
    rel = d.get("relation")
    if rel is None:
        return
    new_rel, auto_rationale = _translate_relation(rel)
    d["relation"] = new_rel
    if auto_rationale and not d.get("rationale"):
        d["rationale"] = auto_rationale


def vocabulary_pass(path, *, dry_run=False):
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    stats = defaultdict(int)
    if raw.get("schema_version") != 1:
        raw["schema_version"] = 1

    for f in raw.get("files", []) or []:
        if isinstance(f, dict):
            old = f.get("relation")
            _migrate_file_or_symbol_relation(f)
            if old != f.get("relation"):
                stats["files_translated"] += 1

    for s in raw.get("symbols", []) or []:
        if not isinstance(s, dict):
            continue
        old_sym = s.get("relation")
        _migrate_file_or_symbol_relation(s)
        if old_sym != s.get("relation"):
            stats["symbols_translated"] += 1
        if "upstream" in s:
            s["upstream"] = _migrate_symbol_ref(s["upstream"])
        if "downstream" in s and s["downstream"] is not None:
            s["downstream"] = _migrate_symbol_ref(s["downstream"])
        if isinstance(s.get("members"), list):
            new_members = []
            for m in s["members"]:
                if isinstance(m, dict):
                    nm = _migrate_member(m)
                    if nm is None:
                        stats["downstream_only_members_dropped"] += 1
                        continue
                    new_members.append(nm)
                    stats["members_translated"] += 1
                else:
                    new_members.append(m)
            s["members"] = new_members

    if not dry_run:
        path.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return stats


# ---- Pass 2 — IR-based downstream-only recovery --------------------------


def _public_member_names_from_ir(ir_symbol):
    out = set()
    for m in ir_symbol.get("members", []):
        if m.get("kind") not in (
            "method", "property", "staticmethod", "classmethod",
            "field", "constant", "enum_value",
        ):
            continue
        name = m.get("name", "")
        if name.startswith("_") and name != "__init__":
            continue
        if m.get("visibility") == "private":
            continue
        out.add(name)
    return out


def _index_ir_by_qualified(ir_path):
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    out = {}
    for f in ir.get("files", []):
        for s in f.get("symbols", []):
            out[s["qualified"]] = s
    return out


def ir_recovery_pass(path, ir_index, *, dry_run=False):
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    added = 0
    for s in raw.get("symbols", []) or []:
        if not isinstance(s, dict):
            continue
        ds = s.get("downstream") or {}
        ds_q = ds.get("qualified") if isinstance(ds, dict) else None
        if not ds_q:
            continue
        ir_sym = ir_index.get(ds_q)
        if ir_sym is None:
            continue
        existing = {
            m.get("downstream")
            for m in s.get("members", []) or []
            if isinstance(m, dict)
        }
        public = _public_member_names_from_ir(ir_sym)
        missing = public - existing - {"__init__"}
        if not missing:
            continue
        s.setdefault("members", [])
        for name in sorted(missing):
            s["members"].append({
                "upstream": f"<downstream-only:{name}>",
                "downstream": name,
                "relation": "divergent",
                "notes": _DOWNSTREAM_ONLY_RATIONALE,
            })
            added += 1
    if added > 0 and not dry_run:
        path.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return added


# ---- Pass 3 — git-history downstream-only recovery -----------------------


def _git_show(ref, path):
    return subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "show", f"{ref}:{path}"],
        text=True, encoding="utf-8",
    )


def _index_pilot_dropped_rows(ref):
    out = {}
    for shard_path in sorted(MAP_DIR.glob("*.yaml")):
        rel = str(shard_path.relative_to(REPO_ROOT)).replace("\\", "/")
        try:
            text = _git_show(ref, rel)
        except subprocess.CalledProcessError:
            continue
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        for s in data.get("symbols", []) or []:
            if not isinstance(s, dict):
                continue
            ds = s.get("downstream") or {}
            ds_q = ds.get("qualified") if isinstance(ds, dict) else None
            if not ds_q:
                continue
            for m in s.get("members", []) or []:
                if isinstance(m, dict) and m.get("relation") == "downstream-only":
                    ds_name = m.get("downstream")
                    if ds_name:
                        out.setdefault(ds_q, []).append({"downstream": ds_name})
    return out


def git_history_recovery_pass(path, dropped_index, *, dry_run=False):
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    added = 0
    for s in raw.get("symbols", []) or []:
        if not isinstance(s, dict):
            continue
        ds = s.get("downstream") or {}
        ds_q = ds.get("qualified") if isinstance(ds, dict) else None
        if not ds_q:
            continue
        dropped = dropped_index.get(ds_q)
        if not dropped:
            continue
        s.setdefault("members", [])
        existing = {
            m.get("downstream")
            for m in s["members"]
            if isinstance(m, dict)
        }
        for dr in dropped:
            ds_name = dr["downstream"]
            if ds_name in existing:
                continue
            s["members"].append({
                "upstream": f"<downstream-only:{ds_name}>",
                "downstream": ds_name,
                "relation": "divergent",
                "notes": _DOWNSTREAM_ONLY_RATIONALE,
            })
            existing.add(ds_name)
            added += 1
    if added > 0 and not dry_run:
        path.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return added


# ---- CLI -----------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--downstream-ir", type=Path,
        help="Downstream IR JSON for pass 2 (IR-based recovery).",
    )
    parser.add_argument(
        "--pilot-ref", default=DEFAULT_PILOT_REF,
        help=f"Git ref of pre-migration pilot maps (default: {DEFAULT_PILOT_REF}).",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not MAP_DIR.is_dir():
        print(f"FATAL: map directory not found: {MAP_DIR}", file=sys.stderr)
        return 2

    print("=== Pass 1: vocabulary translation ===")
    total = defaultdict(int)
    for path in sorted(MAP_DIR.glob("*.yaml")):
        stats = vocabulary_pass(path, dry_run=args.dry_run)
        action = "DRY-RUN" if args.dry_run else "WROTE"
        print(
            f"  {action} {path.name}: "
            f"files={stats['files_translated']} "
            f"symbols={stats['symbols_translated']} "
            f"members={stats['members_translated']} "
            f"members_dropped={stats['downstream_only_members_dropped']}"
        )
        for k, v in stats.items():
            total[k] += v
    print(
        f"  TOTAL: files={total['files_translated']} "
        f"symbols={total['symbols_translated']} "
        f"members={total['members_translated']} "
        f"(dropped: {total['downstream_only_members_dropped']})"
    )

    if args.downstream_ir:
        print()
        print("=== Pass 2: IR-based downstream-only recovery ===")
        if not args.downstream_ir.is_file():
            print(
                f"  WARN: IR not found at {args.downstream_ir}; skipping",
                file=sys.stderr,
            )
        else:
            ir_index = _index_ir_by_qualified(args.downstream_ir)
            recovered = 0
            for path in sorted(MAP_DIR.glob("*.yaml")):
                added = ir_recovery_pass(path, ir_index, dry_run=args.dry_run)
                if added:
                    print(f"  +{added:3d} rows  {path.name}")
                recovered += added
            print(f"  TOTAL: {recovered} rows recovered from IR")

    print()
    print(
        f"=== Pass 3: git-history downstream-only recovery (ref {args.pilot_ref}) ==="
    )
    try:
        dropped_index = _index_pilot_dropped_rows(args.pilot_ref)
    except subprocess.CalledProcessError as e:
        print(f"  WARN: git show failed; skipping ({e})", file=sys.stderr)
    else:
        recovered = 0
        for path in sorted(MAP_DIR.glob("*.yaml")):
            added = git_history_recovery_pass(
                path, dropped_index, dry_run=args.dry_run
            )
            if added:
                print(f"  +{added:3d} rows  {path.name}")
            recovered += added
        total_dropped = sum(len(v) for v in dropped_index.values())
        print(
            f"  TOTAL: {recovered} additional rows recovered from git "
            f"history ({total_dropped - recovered} were already covered)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
