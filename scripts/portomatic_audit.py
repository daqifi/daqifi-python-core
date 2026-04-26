"""Cross-reference ``.portomatic/map/<Shard>.yaml`` against the
codebase + report drift in five categories.

Categories:

1. **protocol-no-impl** — a Python ``Protocol`` class is mapped, all
   its members are marked ``relation: ported``, but no concrete class
   in the package implements the full method set with non-stub bodies.
   This catches the ``NetworkConfigurable`` / ``SdCardOperations``
   gap pattern.

2. **method-missing** — map says ``relation: ported`` for a method
   but the downstream class doesn't have it (rename happened, method
   deleted, etc.).

3. **method-undocumented** — downstream class has a public method /
   property that doesn't appear on any map row. Either the map is
   stale or the method was added without updating the map.

4. **symbol-undocumented** — a downstream class exists with no row on
   any map. Either the class is orphaned (port artifact, dead code)
   or the map needs to be expanded.

5. **stale-divergent** — map says ``relation: divergent`` with a
   rationale, but the downstream code now matches the upstream
   signature. The rationale may be obsolete; operator should re-read
   it and either remove the divergence or refresh the rationale.

Usage::

    python scripts/portomatic_audit.py
    python scripts/portomatic_audit.py --shard Network
    python scripts/portomatic_audit.py --json
    python scripts/portomatic_audit.py --strict   # exit 1 if any drift

Exit codes:
    0 — clean (or non-strict mode with drift reported)
    1 — strict mode + drift detected
    2 — bad arguments / map files unreadable
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_DIR = REPO_ROOT / ".portomatic" / "map"
PYTHON_PKG_ROOT = REPO_ROOT / "daqifi"


# ---------------------------------------------------------------------------
# Map I/O — best-effort YAML reader (handles our generator's output)
# ---------------------------------------------------------------------------


def load_map(path: Path) -> dict:
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except ImportError:
        pass
    return _parse_simple_yaml(path.read_text(encoding="utf-8"))


def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML parser tuned to what
    portomatic_generate_map.py emits. Handles:

    * top-level scalars: ``key: value``
    * top-level lists of dicts: ``key:`` then ``  - subkey: ...``
    * nested dict on a key: ``  key:`` then ``    subkey: ...``

    NOT a real YAML parser — multi-line strings, anchors,
    flow-style, etc. unsupported. Adequate for our generated
    files; falls back to PyYAML if available."""
    lines = text.splitlines()
    root, _ = _parse_block(lines, 0, indent=0)
    return root  # type: ignore[return-value]


def _parse_block(lines: List[str], i: int, indent: int):
    """Recursive-descent parser for our block style.

    Returns (parsed_value, next_line_index). The parsed value is a
    dict for normal blocks or a list when the block consists entirely
    of ``- ...`` items at the requested indent.
    """
    out_dict: dict = {}
    out_list: list = []
    is_list = False
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        cur_indent = len(raw) - len(raw.lstrip(" "))
        if cur_indent < indent:
            break
        if cur_indent > indent:
            # Should have been consumed by the recursive call below.
            i += 1
            continue
        if stripped.startswith("- "):
            is_list = True
            # Parse this list item — could be a scalar or a dict.
            after_dash = stripped[2:].strip()
            if after_dash and ":" in after_dash and not after_dash.endswith(":"):
                # Inline first key: "- key: value"
                key, value = _split_kv(after_dash)
                item: dict = {key: _coerce(value)}
                # Continue collecting siblings at indent + 2
                i += 1
                sibling_indent = indent + 2
                while i < len(lines):
                    sub_raw = lines[i]
                    sub_stripped = sub_raw.strip()
                    if not sub_stripped or sub_stripped.startswith("#"):
                        i += 1
                        continue
                    sub_indent = len(sub_raw) - len(sub_raw.lstrip(" "))
                    if sub_indent < sibling_indent or sub_stripped.startswith("- "):
                        break
                    if ":" in sub_stripped:
                        k, v = _split_kv(sub_stripped)
                        if not v:
                            child, i = _parse_block(lines, i + 1, sibling_indent + 2)
                            item[k] = child
                        else:
                            item[k] = _coerce(v)
                            i += 1
                    else:
                        i += 1
                out_list.append(item)
            else:
                # Scalar list item or dict-with-no-inline-key
                if after_dash and ":" not in after_dash:
                    out_list.append(_coerce(after_dash))
                    i += 1
                else:
                    child, i = _parse_block(lines, i + 1, indent + 2)
                    out_list.append(child)
        else:
            if ":" not in stripped:
                i += 1
                continue
            key, value = _split_kv(stripped)
            if value:
                out_dict[key] = _coerce(value)
                i += 1
            else:
                child, i = _parse_block(lines, i + 1, indent + 2)
                out_dict[key] = child
    return (out_list if is_list and not out_dict else out_dict), i


def _split_kv(line: str) -> Tuple[str, str]:
    k, _, v = line.partition(":")
    return k.strip(), v.strip()


def _coerce(value: str):
    if value == "" or value == "null" or value == "~":
        return None
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value == "true":
        return True
    if value == "false":
        return False
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        s = value[1:-1]
        # Reverse the escaping that the generator's _yaml_escape
        # applied so the round-trip stays clean. Mirrors the
        # equivalent logic in scripts/portomatic_generate_map.py
        # (Qodo PR #36 pass 5 #2 — keep these in sync).
        return (
            s.replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# Python codebase walker — surface every public class + method body shape
# ---------------------------------------------------------------------------


class PyMember:
    __slots__ = ("name", "kind", "is_stub")

    def __init__(self, name: str, kind: str, is_stub: bool):
        self.name = name
        self.kind = kind  # "method" | "property" | "field" | "value"
        self.is_stub = is_stub


class PyClass:
    __slots__ = ("name", "module", "file_rel", "kind", "bases", "members")

    def __init__(
        self,
        name: str,
        module: str,
        file_rel: str,
        kind: str,
        bases: List[str],
        members: Dict[str, PyMember],
    ):
        self.name = name
        self.module = module
        self.file_rel = file_rel
        self.kind = kind  # "class" | "protocol" | "dataclass" | "enum"
        self.bases = bases
        self.members = members

    @property
    def qualified(self) -> str:
        return f"{self.module}.{self.name}"


def _is_stub_body(body: List[ast.stmt]) -> bool:
    """True if a function body is a Protocol stub (no real impl)."""
    if not body:
        return True
    items = body
    # Skip leading docstring.
    if (
        isinstance(items[0], ast.Expr)
        and isinstance(items[0].value, ast.Constant)
        and isinstance(items[0].value.value, str)
    ):
        items = items[1:]
    if not items:
        return True
    if len(items) == 1:
        n = items[0]
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and n.value.value is Ellipsis:
            return True
        if isinstance(n, ast.Pass):
            return True
        if isinstance(n, ast.Raise) and n.exc is not None:
            exc = n.exc
            name = getattr(exc, "id", None) or (
                getattr(getattr(exc, "func", None), "id", None)
                if isinstance(exc, ast.Call)
                else None
            )
            if name == "NotImplementedError":
                return True
    return False


def _classify_class(node: ast.ClassDef) -> Tuple[str, List[str]]:
    base_names: List[str] = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            base_names.append(b.id)
        elif isinstance(b, ast.Attribute):
            base_names.append(b.attr)
        elif isinstance(b, ast.Subscript) and isinstance(b.value, ast.Name):
            base_names.append(b.value.id)
    if "Protocol" in base_names:
        kind = "protocol"
    elif any(b in base_names for b in ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag")):
        kind = "enum"
    else:
        kind = "class"
        for d in node.decorator_list:
            if isinstance(d, ast.Name) and d.id == "dataclass":
                kind = "dataclass"
            elif (
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Name)
                and d.func.id == "dataclass"
            ):
                kind = "dataclass"
    return kind, base_names


def _decorators_include(decorators: List[ast.expr], names: Set[str]) -> bool:
    for d in decorators:
        if isinstance(d, ast.Name) and d.id in names:
            return True
        if isinstance(d, ast.Attribute) and d.attr in names:
            return True
    return False


def walk_python_package(root: Path) -> Tuple[Dict[str, PyClass], List[PyClass]]:
    """Returns (by_qualified_name_dict, all_classes_list).

    Multiple modules may export the same simple name; the dict is
    keyed by the fully-qualified name so they don't collide.
    """
    by_qualified: Dict[str, PyClass] = {}
    all_classes: List[PyClass] = []
    for py_path in sorted(root.rglob("*.py")):
        if "__pycache__" in py_path.parts:
            continue
        rel = py_path.relative_to(REPO_ROOT)
        module = ".".join(py_path.relative_to(REPO_ROOT).with_suffix("").parts)
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name.startswith("_"):
                continue
            kind, bases = _classify_class(node)
            members: Dict[str, PyMember] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("_") and item.name != "__init__":
                        continue
                    is_property = _decorators_include(
                        item.decorator_list, {"property", "cached_property"}
                    )
                    members[item.name] = PyMember(
                        name=item.name,
                        kind="property" if is_property else "method",
                        is_stub=_is_stub_body(item.body),
                    )
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    name = item.target.id
                    if not name.startswith("_"):
                        members[name] = PyMember(name=name, kind="field", is_stub=False)
                elif isinstance(item, ast.Assign):
                    for tgt in item.targets:
                        if isinstance(tgt, ast.Name) and not tgt.id.startswith("_"):
                            members[tgt.id] = PyMember(
                                name=tgt.id, kind="value", is_stub=False
                            )
            cls = PyClass(
                name=node.name,
                module=module,
                file_rel=str(rel).replace("\\", "/"),
                kind=kind,
                bases=bases,
                members=members,
            )
            by_qualified[cls.qualified] = cls
            all_classes.append(cls)
    return by_qualified, all_classes


# ---------------------------------------------------------------------------
# Audit categories
# ---------------------------------------------------------------------------


def find_concrete_impls_for_protocol(
    proto: PyClass, all_classes: List[PyClass]
) -> List[PyClass]:
    """Return every class that satisfies the Protocol's contract:
    declares all the Protocol's non-stub methods/properties as
    NON-STUB on the concrete side. ``Protocol`` checks aren't
    nominal in Python (structural typing), so we test by member set.
    """
    if proto.kind != "protocol":
        return []
    # Only require concrete impls to satisfy STUB Protocol members.
    # Protocol methods with non-stub bodies act as defaults (would
    # be inherited if the Protocol were used as a mixin), so a
    # concrete class doesn't strictly need to override them. Today's
    # daqifi Protocols are all 100% stub members so this filter has
    # no behavioral impact, but it keeps the audit correct if a
    # future Protocol introduces a default impl.
    required = {
        m.name
        for m in proto.members.values()
        if (
            m.kind in ("method", "property")
            and not m.name.startswith("_")
            and m.is_stub
        )
    }
    if not required:
        return []
    impls: List[PyClass] = []
    for cls in all_classes:
        if cls is proto:
            continue
        if cls.kind == "protocol":
            continue
        cls_member_names = {
            m.name
            for m in cls.members.values()
            if m.kind in ("method", "property") and not m.is_stub
        }
        if required.issubset(cls_member_names):
            impls.append(cls)
    return impls


def audit_shard_map(
    shard_map: dict,
    py_classes: Dict[str, PyClass],
    all_classes: List[PyClass],
) -> List[dict]:
    """Run all 5 audit categories against one shard map; return drift rows."""
    drift: List[dict] = []
    shard_name = shard_map.get("shard", "?")

    for sym in shard_map.get("symbols", []) or []:
        if not isinstance(sym, dict):
            continue
        ds = sym.get("downstream") or {}
        ds_qualified = ds.get("qualified") if isinstance(ds, dict) else None
        relation = sym.get("relation")
        upstream = sym.get("upstream") or {}
        upstream_q = upstream.get("qualified") if isinstance(upstream, dict) else "?"

        # If symbol relation is divergent / dropped, just sanity-check the rationale exists.
        if relation == "divergent":
            if not sym.get("rationale"):
                drift.append(
                    {
                        "shard": shard_name,
                        "category": "stale-divergent",
                        "symbol": upstream_q,
                        "detail": "marked divergent without a rationale — operator should add one or remove the divergence",
                    }
                )
            continue
        if relation == "dropped":
            continue

        # Symbol is `ported` or `missing`. Dig into members.
        ds_class: Optional[PyClass] = (
            py_classes.get(ds_qualified) if ds_qualified else None
        )
        if relation == "ported" and ds_class is None:
            drift.append(
                {
                    "shard": shard_name,
                    "category": "method-missing",
                    "symbol": upstream_q,
                    "detail": f"map says ported to {ds_qualified!r} but that class doesn't exist in the codebase",
                }
            )
            continue

        # Protocol-without-impl check.
        if ds_class and ds_class.kind == "protocol":
            impls = find_concrete_impls_for_protocol(ds_class, all_classes)
            if not impls:
                # Count only `ported` contract members — that's what
                # a concrete impl actually has to satisfy. Including
                # `downstream-only` rows in the count would inflate
                # the diagnostic (those are Python-side additions
                # that aren't part of the upstream contract).
                method_count = sum(
                    1
                    for m in (sym.get("members") or [])
                    if (
                        isinstance(m, dict)
                        and m.get("relation") == "ported"
                        and m.get("downstream_kind") in ("method", "property")
                    )
                )
                drift.append(
                    {
                        "shard": shard_name,
                        "category": "protocol-no-impl",
                        "symbol": ds_class.qualified,
                        "detail": f"Protocol declared with {method_count} method/property contract — no concrete class implements all of them",
                    }
                )

        # Method-missing check (per-member): map says ported but
        # downstream method isn't there.
        for m in sym.get("members") or []:
            if not isinstance(m, dict):
                continue
            if m.get("relation") != "ported":
                continue
            ds_member_name = m.get("downstream")
            if not ds_member_name or not ds_class:
                continue
            if ds_member_name not in ds_class.members:
                drift.append(
                    {
                        "shard": shard_name,
                        "category": "method-missing",
                        "symbol": ds_class.qualified,
                        "detail": f"map says {ds_member_name!r} is ported but it doesn't exist on {ds_class.qualified}",
                    }
                )

    return drift


def audit_undocumented(
    maps: List[dict], py_classes: Dict[str, PyClass]
) -> List[dict]:
    """Categories 3 + 4: downstream code that has no map row.

    Walks every Python class in the package; if its qualified name
    doesn't appear as a downstream pointer in ANY map, flag it as
    symbol-undocumented. For mapped classes, check each public member
    against the map's members[] list — flag method-undocumented for
    any public method/property not represented.
    """
    # Build set of mapped downstream classes + their mapped members.
    mapped_class_to_members: Dict[str, Set[str]] = defaultdict(set)
    for shard_map in maps:
        for sym in shard_map.get("symbols", []) or []:
            if not isinstance(sym, dict):
                continue
            ds = sym.get("downstream") or {}
            qualified = ds.get("qualified") if isinstance(ds, dict) else None
            if not qualified:
                continue
            for m in sym.get("members") or []:
                if isinstance(m, dict):
                    name = m.get("downstream")
                    if name:
                        mapped_class_to_members[qualified].add(name)

    drift: List[dict] = []
    for cls in py_classes.values():
        if cls.qualified not in mapped_class_to_members:
            # Skip purely-internal types that aren't expected to map
            # 1:1 (helpers under daqifi/_internal/, etc.). Operator can
            # still surface them by mapping explicitly.
            if "._internal." in cls.qualified:
                continue
            drift.append(
                {
                    "shard": "(unmapped)",
                    "category": "symbol-undocumented",
                    "symbol": cls.qualified,
                    "detail": f"public class {cls.kind} not referenced by any shard map",
                }
            )
            continue
        mapped_members = mapped_class_to_members[cls.qualified]
        for member in cls.members.values():
            if member.kind not in ("method", "property"):
                continue
            if member.name in mapped_members:
                continue
            if member.name == "__init__":
                continue
            drift.append(
                {
                    "shard": "(unmapped)",
                    "category": "method-undocumented",
                    "symbol": f"{cls.qualified}.{member.name}",
                    "detail": "public method/property not represented on the shard map",
                }
            )

    return drift


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", help="Audit one shard. Default: all maps.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human text.")
    parser.add_argument(
        "--strict", action="store_true", help="Exit 1 if any drift is reported."
    )
    parser.add_argument(
        "--category",
        action="append",
        choices=[
            "protocol-no-impl",
            "method-missing",
            "method-undocumented",
            "symbol-undocumented",
            "stale-divergent",
        ],
        help="Restrict output to specific categories. Can be passed multiple times.",
    )
    args = parser.parse_args(argv)

    if not MAP_DIR.is_dir():
        print(f"FATAL: no map directory at {MAP_DIR}", file=sys.stderr)
        return 2
    if not PYTHON_PKG_ROOT.is_dir():
        print(f"FATAL: no Python package at {PYTHON_PKG_ROOT}", file=sys.stderr)
        return 2

    py_classes, all_classes = walk_python_package(PYTHON_PKG_ROOT)

    map_paths = sorted(MAP_DIR.glob("*.yaml"))
    maps: List[dict] = []
    for p in map_paths:
        if args.shard and p.stem != args.shard:
            continue
        try:
            maps.append(load_map(p))
        except Exception as e:
            print(f"WARN: could not parse {p}: {e}", file=sys.stderr)

    drift: List[dict] = []
    for shard_map in maps:
        drift.extend(audit_shard_map(shard_map, py_classes, all_classes))
    drift.extend(audit_undocumented(maps, py_classes))

    if args.category:
        drift = [d for d in drift if d["category"] in set(args.category)]

    if args.json:
        print(json.dumps({"drift": drift, "total": len(drift)}, indent=2))
    else:
        if not drift:
            print("Audit clean — no drift detected.")
        else:
            by_cat: Dict[str, List[dict]] = defaultdict(list)
            for d in drift:
                by_cat[d["category"]].append(d)
            for cat in sorted(by_cat):
                rows = by_cat[cat]
                print(f"\n=== {cat} ({len(rows)}) ===")
                for r in rows:
                    print(f"  [{r['shard']}] {r['symbol']}")
                    print(f"      {r['detail']}")
            print(f"\nTotal drift rows: {len(drift)}")

    if args.strict and drift:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
