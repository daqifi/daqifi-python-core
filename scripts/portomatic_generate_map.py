"""Generate / refresh ``.portomatic/map/<Shard>.yaml`` shard maps.

Walks the C# upstream + Python downstream and emits one map per shard
with file-, symbol-, and member-level rows. Schema v2 (see
cptkoolbeenz/portomatic#147).

Design choices:

* C# parsing is regex-based. A real Roslyn parser would be more
  robust but is overkill for the declarative member surface we care
  about (class/interface/struct/record/enum + their public methods +
  public properties). When the regex misses an exotic construct, the
  generator emits a TODO comment in the map so the operator notices.
* Python parsing uses the stdlib ``ast`` module — accurate, fast,
  no external deps.
* Maps are MERGED with existing on-disk contents. Manual fields
  (rationale, notes, accepted divergences, hal_pairs, backlog) are
  preserved across regeneration. Auto-discovered rows that get
  removed upstream are kept in a ``deprecated:`` block for one cycle
  so an operator can review before deleting.
* Default name-mapping heuristic: C# ``CamelCase`` → Python
  ``snake_case``; C# ``IFoo`` interface → Python ``Foo`` Protocol;
  C# ``Foo<T>`` generic → Python ``Foo`` (generics collapse).
* When a downstream symbol can't be resolved by name, the generator
  marks the row ``relation: missing`` rather than guessing — operator
  decides whether to map manually or leave as a real gap.

Usage::

    python scripts/portomatic_generate_map.py --all
    python scripts/portomatic_generate_map.py --shard Network
    python scripts/portomatic_generate_map.py --shard Network --dry-run
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_DIR = REPO_ROOT / ".portomatic" / "map"
PYTHON_PKG_ROOT = REPO_ROOT / "daqifi"
UPSTREAM_ROOT = (
    REPO_ROOT.parent / "daqifi-core" / "src" / "Daqifi.Core"
).resolve()


# Shard layout. Each entry maps a shard name to the upstream subdirectory
# under daqifi-core/src/Daqifi.Core/. The downstream side is the whole
# daqifi/ Python package — the generator finds matches by name.
#
# The upstream tree is not strictly two-level; some "shards" are
# subdirectories of Device/. We list each meaningful one separately so
# the operator can scope edits per shard.
SHARDS: Dict[str, str] = {
    "Channel": "Channel",
    "Communication": "Communication",
    "Communication.Consumers": "Communication/Consumers",
    "Communication.Messages": "Communication/Messages",
    "Communication.Producers": "Communication/Producers",
    "Communication.Transport": "Communication/Transport",
    "Device": "Device",
    "Discovery": "Device/Discovery",
    "Network": "Device/Network",
    "Protocol": "Device/Protocol",
    "SdCard": "Device/SdCard",
    "Firmware": "Firmware",
}


# ---------------------------------------------------------------------------
# C# parsing (regex-based, declarative-only)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class CsMember:
    name: str
    kind: str  # "method" | "property" | "field" | "event" | "ctor"
    signature: str  # the trimmed source line(s) for context


@dataclasses.dataclass
class CsSymbol:
    qualified: str  # e.g. Daqifi.Core.Device.Network.INetworkConfigurable
    name: str  # e.g. INetworkConfigurable
    kind: str  # "interface" | "class" | "struct" | "record" | "enum"
    file: Path  # absolute path
    file_rel: str  # e.g. src/Daqifi.Core/Device/Network/INetworkConfigurable.cs
    members: List[CsMember]


# Strip strings + comments (line + block) before scanning for members.
_CS_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CS_COMMENT_LINE_RE = re.compile(r"//[^\n]*")
_CS_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')

_CS_NAMESPACE_RE = re.compile(r"\bnamespace\s+([\w.]+)")
_CS_TYPE_DECL_RE = re.compile(
    r"\bpublic\s+(?:abstract\s+|sealed\s+|static\s+|partial\s+)?"
    r"(class|interface|struct|record|enum)\s+(\w+)"
)
# Method regex; the leading ``public`` is OPTIONAL when scanning the
# body of an interface (where members are implicitly public).
# Switching is handled at call site by passing ``in_interface``.
_CS_METHOD_PUBLIC_RE = re.compile(
    r"\bpublic\s+(?:static\s+|virtual\s+|override\s+|abstract\s+|async\s+|extern\s+|sealed\s+|new\s+|unsafe\s+)*"
    r"(?!class\b|interface\b|struct\b|record\b|enum\b)"
    r"[\w<>\[\],?.:\s]+?\s+"
    r"(\w+)\s*\("
)
_CS_METHOD_INTERFACE_RE = re.compile(
    r"(?:^|\n)\s*"
    r"(?:async\s+)?"  # async methods only on default-impl interfaces, but allow
    r"(?!//|/\*|class\b|interface\b|struct\b|record\b|enum\b|using\b|namespace\b)"
    r"[\w<>\[\],?.:]+\s+"
    r"(\w+)\s*\("
)
_CS_PROPERTY_PUBLIC_RE = re.compile(
    r"\bpublic\s+(?:static\s+|virtual\s+|override\s+|abstract\s+|new\s+)*"
    r"[\w<>\[\],?.:\s]+?\s+(\w+)\s*\{\s*get"
)
_CS_PROPERTY_INTERFACE_RE = re.compile(
    r"(?:^|\n)\s*"
    r"(?!//|/\*)"
    r"[\w<>\[\],?.:]+\s+(\w+)\s*\{\s*get"
)
_CS_EVENT_PUBLIC_RE = re.compile(
    r"\bpublic\s+(?:static\s+|virtual\s+|abstract\s+|new\s+)*event\s+[\w<>\[\],?.:\s]+\s+(\w+)\s*[;{]"
)
_CS_EVENT_INTERFACE_RE = re.compile(
    r"(?:^|\n)\s*event\s+[\w<>\[\],?.:\s]+\s+(\w+)\s*[;{]"
)
# Enum values: simple identifiers separated by commas inside an enum body.
_CS_ENUM_VALUE_RE = re.compile(r"(?:^|,|\{)\s*(\w+)(?:\s*=\s*[^,}]+)?\s*(?=,|\})")


def _strip_cs_noise(src: str) -> str:
    """Strip comments + string literals so member regexes don't false-match
    on text inside docstrings / XML-doc / strings."""
    src = _CS_COMMENT_BLOCK_RE.sub("", src)
    src = _CS_COMMENT_LINE_RE.sub("", src)
    src = _CS_STRING_RE.sub('""', src)
    return src


def parse_cs_file(path: Path) -> List[CsSymbol]:
    """Extract every public type + its public members from a C# file.

    Approach: strip noise, find the namespace, then scan for type
    declarations. For each declaration, slice the body (matched
    braces) and scan it for members. Doesn't try to handle nested
    types — none in the daqifi-core surface.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    src = _strip_cs_noise(raw)
    ns_match = _CS_NAMESPACE_RE.search(src)
    namespace = ns_match.group(1) if ns_match else ""
    file_rel = str(path.relative_to(UPSTREAM_ROOT.parent.parent))
    symbols: List[CsSymbol] = []

    for m in _CS_TYPE_DECL_RE.finditer(src):
        kind = m.group(1)
        name = m.group(2)
        body = _slice_brace_body(src, m.end())
        members = (
            _extract_cs_members(body, type_name=name, kind=kind) if body else []
        )
        symbols.append(
            CsSymbol(
                qualified=f"{namespace}.{name}" if namespace else name,
                name=name,
                kind=kind,
                file=path,
                file_rel=file_rel,
                members=members,
            )
        )
    return symbols


def _slice_brace_body(src: str, start: int) -> Optional[str]:
    """Find the next ``{`` after start and return the matched-brace body."""
    open_idx = src.find("{", start)
    if open_idx == -1:
        return None
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1 : i]
    return None


def _extract_cs_members(body: str, type_name: str, kind: str) -> List[CsMember]:
    """Extract methods / properties / events / enum values from a type body.

    Picks the right regex set based on the enclosing kind:
      * ``interface`` — members have no access modifier (implicitly public)
      * ``enum`` — values are bare identifiers separated by commas
      * everything else — members must be ``public``

    Constructor detection: a public X(...) where X matches the type
    name. Tagged as kind="ctor".
    """
    if kind == "enum":
        members: List[CsMember] = []
        for m in _CS_ENUM_VALUE_RE.finditer(body):
            members.append(
                CsMember(name=m.group(1), kind="value", signature=_trim(m.group(0)))
            )
        return members

    in_interface = kind == "interface"
    prop_re = _CS_PROPERTY_INTERFACE_RE if in_interface else _CS_PROPERTY_PUBLIC_RE
    event_re = _CS_EVENT_INTERFACE_RE if in_interface else _CS_EVENT_PUBLIC_RE
    method_re = _CS_METHOD_INTERFACE_RE if in_interface else _CS_METHOD_PUBLIC_RE

    seen: set[Tuple[str, str]] = set()
    members = []

    for m in prop_re.finditer(body):
        name = m.group(1)
        key = ("property", name)
        if key in seen:
            continue
        seen.add(key)
        members.append(
            CsMember(name=name, kind="property", signature=_trim(m.group(0)))
        )

    for m in event_re.finditer(body):
        name = m.group(1)
        key = ("event", name)
        if key in seen:
            continue
        seen.add(key)
        members.append(
            CsMember(name=name, kind="event", signature=_trim(m.group(0)))
        )

    body_no_props = prop_re.sub("", body)
    for m in method_re.finditer(body_no_props):
        name = m.group(1)
        member_kind = "ctor" if name == type_name else "method"
        key = (member_kind, name)
        if key in seen:
            continue
        # Drop common noise tokens that the loose interface regex picks up.
        if name in {"get", "set", "if", "while", "for", "return", "var", "switch", "throw", "new"}:
            continue
        seen.add(key)
        members.append(
            CsMember(name=name, kind=member_kind, signature=_trim(m.group(0)))
        )

    return members


def _trim(s: str, limit: int = 120) -> str:
    s = " ".join(s.split())
    if len(s) > limit:
        s = s[: limit - 3] + "..."
    return s


# ---------------------------------------------------------------------------
# Python parsing (AST)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PySymbol:
    qualified: str  # e.g. daqifi.network.NetworkConfigurable
    name: str  # e.g. NetworkConfigurable
    kind: str  # "class" | "protocol" | "dataclass" | "enum"
    module: str  # daqifi.network
    file_rel: str  # daqifi/network.py
    members: List[str]  # method + property names


def parse_python_package(root: Path) -> Dict[str, PySymbol]:
    """Walk daqifi/ + return every public class keyed by simple name.

    Multiple modules may export the same simple name (e.g.
    ``NetworkConfiguration``); we keep the first encountered and put
    duplicates under ``f"{name}@{module}"`` so the audit can flag the
    ambiguity.
    """
    out: Dict[str, PySymbol] = {}
    for py_path in sorted(root.rglob("*.py")):
        if "__pycache__" in py_path.parts:
            continue
        rel = py_path.relative_to(REPO_ROOT)
        module_path = ".".join(py_path.relative_to(REPO_ROOT).with_suffix("").parts)
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name.startswith("_"):
                continue
            kind = _classify_python_class(node)
            members = _collect_public_methods(node)
            sym = PySymbol(
                qualified=f"{module_path}.{node.name}",
                name=node.name,
                kind=kind,
                module=module_path,
                file_rel=str(rel).replace("\\", "/"),
                members=members,
            )
            key = node.name if node.name not in out else f"{node.name}@{module_path}"
            out[key] = sym
    return out


def _classify_python_class(node: ast.ClassDef) -> str:
    base_names = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            base_names.append(b.id)
        elif isinstance(b, ast.Attribute):
            base_names.append(b.attr)
        elif isinstance(b, ast.Subscript) and isinstance(b.value, ast.Name):
            base_names.append(b.value.id)
    if "Protocol" in base_names:
        return "protocol"
    if any(b in base_names for b in ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag")):
        return "enum"
    for d in node.decorator_list:
        if isinstance(d, ast.Name) and d.id == "dataclass":
            return "dataclass"
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "dataclass":
            return "dataclass"
    return "class"


def _collect_public_methods(node: ast.ClassDef) -> List[str]:
    """Collect every public method, property, dataclass field, and enum
    value name on a Python class — the surface a C# member could map
    to. Skips private (``_foo``) names except dunder ``__init__``."""
    out: List[str] = []
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("_") and item.name != "__init__":
                continue
            out.append(item.name)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            # Dataclass-style annotated field: ``mode: WifiMode = ...``
            name = item.target.id
            if not name.startswith("_"):
                out.append(name)
        elif isinstance(item, ast.Assign):
            # Enum value or class-level constant: ``MODE = "value"``
            for tgt in item.targets:
                if isinstance(tgt, ast.Name) and not tgt.id.startswith("_"):
                    out.append(tgt.id)
    return out


# ---------------------------------------------------------------------------
# Mapping heuristics
# ---------------------------------------------------------------------------


def cs_to_py_type_name(cs_name: str) -> str:
    """C# ``IFoo`` interface → Python ``Foo`` Protocol; otherwise leave as-is."""
    if cs_name.startswith("I") and len(cs_name) > 1 and cs_name[1].isupper():
        return cs_name[1:]
    return cs_name


_PASCAL_TO_SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")


def cs_to_py_member_name(cs_name: str, *, enum_value: bool = False) -> str:
    """Map a C# member name to Python convention.

    * ``UpdateNetworkConfigurationAsync`` → ``update_network_configuration``
      (snake_case + strip ``Async`` suffix)
    * ``Mode`` → ``mode``
    * Enum values use UPPER_SNAKE: ``ExistingNetwork`` → ``EXISTING_NETWORK``
    """
    name = cs_name
    if name.endswith("Async"):
        name = name[: -len("Async")]
    snake = _PASCAL_TO_SNAKE_RE.sub("_", name)
    return snake.upper() if enum_value else snake.lower()


# ---------------------------------------------------------------------------
# Map I/O — minimal hand-rolled YAML writer (avoids a PyYAML dep)
# ---------------------------------------------------------------------------


def _yaml_escape(value: str) -> str:
    """Quote if the value contains characters that would break bare YAML
    (colons, leading/trailing whitespace, special chars, control
    chars). Otherwise emit bare for readability.

    Control-character handling matters because the minimal YAML
    reader in this script + ``portomatic_audit.py`` is line-based —
    a literal newline in a value would split it across two YAML
    "rows" and corrupt the parse on the next regen-then-load cycle.
    """
    if value == "":
        return "''"
    if (
        any(ch in value for ch in ":#&*!|>'\"%@`,?[]{}\n\r\t")
        or value[0] in "-?:"
        or value != value.strip()
    ):
        # Use double-quoted form + escape control chars so the
        # output stays single-line and round-trip safe.
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return value


def _emit_yaml(obj, indent: int = 0) -> List[str]:
    pad = "  " * indent
    lines: List[str] = []
    if isinstance(obj, dict):
        if not obj:
            return [f"{pad}{{}}"]
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.extend(_emit_yaml(v, indent + 1))
            elif isinstance(v, (dict, list)):
                rendered = "{}" if isinstance(v, dict) else "[]"
                lines.append(f"{pad}{k}: {rendered}")
            elif v is None:
                lines.append(f"{pad}{k}: null")
            elif isinstance(v, bool):
                lines.append(f"{pad}{k}: {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{pad}{k}: {v}")
            else:
                lines.append(f"{pad}{k}: {_yaml_escape(str(v))}")
    elif isinstance(obj, list):
        if not obj:
            return [f"{pad}[]"]
        for item in obj:
            if isinstance(item, dict):
                first = True
                for k, v in item.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    first = False
                    if isinstance(v, (dict, list)) and v:
                        lines.append(f"{prefix}{k}:")
                        lines.extend(_emit_yaml(v, indent + 2))
                    elif isinstance(v, (dict, list)):
                        rendered = "{}" if isinstance(v, dict) else "[]"
                        lines.append(f"{prefix}{k}: {rendered}")
                    elif v is None:
                        lines.append(f"{prefix}{k}: null")
                    elif isinstance(v, bool):
                        lines.append(f"{prefix}{k}: {'true' if v else 'false'}")
                    elif isinstance(v, (int, float)):
                        lines.append(f"{prefix}{k}: {v}")
                    else:
                        lines.append(f"{prefix}{k}: {_yaml_escape(str(v))}")
            else:
                lines.append(f"{pad}- {_yaml_escape(str(item))}")
    return lines


def write_yaml(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(_emit_yaml(obj)) + "\n"
    path.write_text(body, encoding="utf-8")


def read_existing_map(path: Path) -> dict:
    """Best-effort load of an existing map. Returns {} if absent or
    unparseable. Uses a minimal YAML reader to avoid the PyYAML dep —
    we only need the top-level key/value structure to preserve manual
    rationale/notes/divergences/backlog/hal_pairs blocks across
    regeneration. Anything we can't parse is dropped to "unknown" and
    the operator can re-paste from git history if needed."""
    if not path.is_file():
        return {}
    # Optional dep: try yaml if available.
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except ImportError:
        pass
    # Fallback: extract preserved sections via crude line scan.
    return _read_yaml_minimal(path)


def _read_yaml_minimal(path: Path) -> dict:
    """Minimal reader: pulls top-level scalar keys + preserves the
    raw text of nested sections we want to round-trip
    (divergences, backlog, hal_pairs, notes, rationale).

    NOT a real YAML parser — best-effort fallback only. Coerces
    scalars to Python types (None/bool/int/float/str) so the
    downstream merge logic, which checks ``if ex.get(key) is not
    None``, works correctly when this fallback path is taken."""
    def _coerce_scalar(value: str):
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
            # Reverse the escaping that _yaml_escape applied so the
            # round-trip stays clean.
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

    out: dict = {}
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line and not line.startswith(" ") and ":" in line:
            k, _, v = line.partition(":")
            v = v.strip()
            if v and not v.endswith(":"):
                out[k.strip()] = _coerce_scalar(v)
    return out


# ---------------------------------------------------------------------------
# Map generation
# ---------------------------------------------------------------------------


def upstream_head_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(UPSTREAM_ROOT.parent.parent), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return ""


def generate_map(shard: str, py_index: Dict[str, PySymbol]) -> dict:
    """Produce a complete map dict for one shard."""
    sub = SHARDS[shard]
    upstream_dir = UPSTREAM_ROOT / sub.replace("/", "/")
    if not upstream_dir.is_dir():
        raise FileNotFoundError(f"upstream shard not found: {upstream_dir}")

    # Walk *.cs in the shard directory (NOT recursive — children belong
    # to their own shard maps).
    cs_files = sorted(upstream_dir.glob("*.cs"))

    # Gather every C# symbol in the shard up front so the per-symbol
    # collapse-detection (interface + concrete-class -> single Python
    # Protocol) has the full sibling list at row-generation time.
    all_cs_symbols_in_shard: List[CsSymbol] = []
    cs_symbols_per_file: Dict[Path, List[CsSymbol]] = {}
    for cs_path in cs_files:
        cs_symbols = parse_cs_file(cs_path)
        cs_symbols_per_file[cs_path] = cs_symbols
        all_cs_symbols_in_shard.extend(cs_symbols)

    files: List[dict] = []
    symbols: List[dict] = []

    for cs_path in cs_files:
        cs_symbols = cs_symbols_per_file[cs_path]
        # Best-effort downstream-file resolution: if any of the C# symbols
        # in this file have a Python match, use that match's file as the
        # downstream pointer. Otherwise leave null.
        ds_files = []
        for cs_sym in cs_symbols:
            py_name = cs_to_py_type_name(cs_sym.name)
            py = py_index.get(py_name)
            if py:
                ds_files.append(py.file_rel)
        ds_file = ds_files[0] if ds_files else None
        files.append(
            {
                "upstream": cs_sym_file_rel(cs_path),
                "downstream": ds_file,
                "relation": "ported" if ds_file else "missing",
                "notes": None,
                "rationale": None,
                "last_synced_sha": None,
            }
        )
        for cs_sym in cs_symbols:
            symbols.append(symbol_row(cs_sym, py_index, all_cs_symbols_in_shard))

    return {
        "schema_version": 2,
        "shard": shard,
        "upstream_root": f"src/Daqifi.Core/{sub}",
        "downstream_root": "daqifi",
        "last_synced_upstream_sha": upstream_head_sha(),
        "files": files,
        "symbols": symbols,
        "divergences": [],
        "backlog": [],
        "hal_pairs": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def cs_sym_file_rel(path: Path) -> str:
    return str(path.relative_to(UPSTREAM_ROOT.parent.parent)).replace("\\", "/")


def _other_cs_symbol_maps_here(
    cs_sym: CsSymbol, py_name: str, all_cs_symbols_in_shard: List[CsSymbol]
) -> bool:
    """True if some OTHER C# symbol in this shard auto-maps to the
    same Python class — typical case is the C# I-prefixed interface
    + the concrete class both name-mapping to a single Python Protocol.

    The Python port collapses C# interface + concrete-class pairs
    into a single Protocol per upstream surface; the concrete C#
    class has no Python equivalent. Marking it divergent here keeps
    the audit's protocol-no-impl signal accurate (the class would
    otherwise look like a Protocol-without-impl at audit time)."""
    for other in all_cs_symbols_in_shard:
        if other is cs_sym:
            continue
        if cs_to_py_type_name(other.name) == py_name and other.kind == "interface":
            return True
    return False


def symbol_row(
    cs_sym: CsSymbol,
    py_index: Dict[str, PySymbol],
    all_cs_symbols_in_shard: Optional[List[CsSymbol]] = None,
) -> dict:
    py_name = cs_to_py_type_name(cs_sym.name)
    py = py_index.get(py_name)
    # Collapse-detection: if a C# class auto-maps to the same Python
    # class as a sibling C# interface, mark the class divergent so we
    # don't double-count the impl. Without this, audits report the
    # Python Protocol as "no concrete impl" because the auto-mapped
    # impl is itself a Protocol (not a real concrete class).
    if (
        py
        and cs_sym.kind in ("class", "struct", "record")
        and py.kind == "protocol"
        and all_cs_symbols_in_shard is not None
        and _other_cs_symbol_maps_here(cs_sym, py_name, all_cs_symbols_in_shard)
    ):
        return {
            "upstream": {
                "kind": cs_sym.kind,
                "qualified": cs_sym.qualified,
                "file": cs_sym_file_rel(cs_sym.file),
            },
            "downstream": None,
            "relation": "divergent",
            "last_synced_sha": None,
            "notes": None,
            "rationale": (
                f"Python collapses the C# {cs_sym.kind} {cs_sym.name} + its "
                f"sibling I{cs_sym.name} interface into a single "
                f"{py.qualified} Protocol. No concrete Python class is "
                f"maintained for the {cs_sym.kind} side."
            ),
            "members": [],
        }
    members = []
    py_methods = set(py.members) if py else set()
    seen_py: set[str] = set()
    for m in cs_sym.members:
        # Constructors don't have a 1:1 Python equivalent (we use
        # __init__ but it's auto-derived from dataclass / hand-written).
        # Mark them divergent-by-convention.
        if m.kind == "ctor":
            members.append(
                {
                    "upstream": m.name,
                    "upstream_kind": m.kind,
                    "downstream": "__init__",
                    "downstream_kind": "method",
                    "relation": "divergent",
                    "rationale": "Python constructors are __init__ — name divergence is conventional.",
                }
            )
            continue
        py_member = cs_to_py_member_name(m.name, enum_value=(m.kind == "value"))
        if py and py_member in py_methods:
            seen_py.add(py_member)
            members.append(
                {
                    "upstream": m.name,
                    "upstream_kind": m.kind,
                    "downstream": py_member,
                    "downstream_kind": "method" if m.kind == "method" else m.kind,
                    "relation": "ported",
                    "rationale": None,
                }
            )
        else:
            members.append(
                {
                    "upstream": m.name,
                    "upstream_kind": m.kind,
                    "downstream": py_member if py else None,
                    "downstream_kind": None,
                    "relation": "missing",
                    "rationale": None,
                }
            )
    # Python-only members on this class — flag as undocumented for the
    # auditor to triage.
    if py:
        for extra in sorted(py_methods - seen_py):
            members.append(
                {
                    "upstream": None,
                    "upstream_kind": None,
                    "downstream": extra,
                    "downstream_kind": "method",
                    "relation": "downstream-only",
                    "rationale": None,
                }
            )

    return {
        "upstream": {
            "kind": cs_sym.kind,
            "qualified": cs_sym.qualified,
            "file": cs_sym_file_rel(cs_sym.file),
        },
        "downstream": {
            "kind": py.kind,
            "qualified": py.qualified,
            "file": py.file_rel,
        }
        if py
        else None,
        "relation": "ported" if py else "missing",
        "last_synced_sha": None,
        "notes": None,
        "rationale": None,
        "members": members,
    }


# ---------------------------------------------------------------------------
# Merge with existing map (preserve manual edits)
# ---------------------------------------------------------------------------


def merge_into_existing(generated: dict, existing: dict) -> dict:
    """Preserve manually-curated fields on existing rows.

    For each ``files`` and ``symbols`` row, look up the matching row
    in the existing map by upstream path/qualified name. If found,
    copy across the human-curated fields (notes, rationale,
    last_synced_sha) before letting the auto-generated fields win on
    everything else.

    For ``divergences`` / ``backlog`` / ``hal_pairs`` — keep the
    existing list verbatim; never auto-populate these from the
    generator."""
    if not existing:
        return generated
    # Files: key by upstream path
    if isinstance(existing.get("files"), list):
        ex_files = {f.get("upstream"): f for f in existing["files"] if isinstance(f, dict)}
        for f in generated["files"]:
            ex = ex_files.get(f["upstream"])
            if not ex:
                continue
            for key in ("notes", "rationale", "last_synced_sha"):
                if ex.get(key) is not None:
                    f[key] = ex[key]
            # Preserve a manual relation override (e.g. "divergent" with rationale).
            # When relation is divergent/dropped, ALSO preserve the
            # original downstream pointer — operator may have set it
            # to null deliberately ("no Python equivalent"), and
            # auto-discovered name matches shouldn't override that.
            if ex.get("relation") in ("divergent", "dropped") and ex.get("rationale"):
                f["relation"] = ex["relation"]
                if "downstream" in ex:
                    f["downstream"] = ex["downstream"]
    if isinstance(existing.get("symbols"), list):
        ex_symbols = {}
        for s in existing["symbols"]:
            if isinstance(s, dict) and isinstance(s.get("upstream"), dict):
                ex_symbols[s["upstream"].get("qualified")] = s
        for s in generated["symbols"]:
            ex = ex_symbols.get(s["upstream"]["qualified"])
            if not ex:
                continue
            for key in ("notes", "rationale", "last_synced_sha"):
                if ex.get(key) is not None:
                    s[key] = ex[key]
            # Same downstream-preservation as above for files: when the
            # operator marked a symbol divergent/dropped (with rationale),
            # honor their downstream pointer (often null) instead of
            # letting the name-match heuristic auto-link to a class
            # they intentionally said doesn't apply.
            if ex.get("relation") in ("divergent", "dropped") and ex.get("rationale"):
                s["relation"] = ex["relation"]
                if "downstream" in ex:
                    s["downstream"] = ex["downstream"]
            # Member-level preservation. Manual ``rationale`` / ``notes``
            # / ``relation`` overrides on individual members must
            # survive regeneration — without this, the operator's
            # decision that a particular method is divergent (with
            # documented reason) gets clobbered the next time the
            # generator runs and finds the C# upstream method exists.
            ex_members = ex.get("members") or []
            if isinstance(ex_members, list) and isinstance(s.get("members"), list):
                # Key by (upstream_name, upstream_kind) so renames in
                # Python-land don't accidentally erase the manual
                # rationale tied to a still-present C# member.
                ex_member_by_key = {
                    (m.get("upstream"), m.get("upstream_kind")): m
                    for m in ex_members
                    if isinstance(m, dict)
                }
                for m in s["members"]:
                    if not isinstance(m, dict):
                        continue
                    exm = ex_member_by_key.get(
                        (m.get("upstream"), m.get("upstream_kind"))
                    )
                    if not exm:
                        continue
                    for key in ("notes", "rationale"):
                        if exm.get(key) is not None:
                            m[key] = exm[key]
                    if (
                        exm.get("relation") in ("divergent", "dropped")
                        and exm.get("rationale")
                    ):
                        m["relation"] = exm["relation"]
    # Verbatim preserve curated lists.
    for key in ("divergences", "backlog", "hal_pairs"):
        if isinstance(existing.get(key), list) and existing[key]:
            generated[key] = existing[key]
    return generated


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", help="Generate / refresh one shard. Default: --all required.")
    parser.add_argument("--all", action="store_true", help="Generate / refresh every known shard.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary; don't write files.")
    args = parser.parse_args(argv)

    if not args.all and not args.shard:
        parser.error("pass --shard <name> or --all")

    if not UPSTREAM_ROOT.is_dir():
        print(f"FATAL: upstream root not found: {UPSTREAM_ROOT}", file=sys.stderr)
        return 2

    py_index = parse_python_package(PYTHON_PKG_ROOT)
    print(f"Indexed {len(py_index)} Python classes from {PYTHON_PKG_ROOT}")

    shards = list(SHARDS) if args.all else [args.shard]
    for shard in shards:
        if shard not in SHARDS:
            print(f"WARN: unknown shard {shard!r}; valid: {sorted(SHARDS)}", file=sys.stderr)
            continue
        try:
            generated = generate_map(shard, py_index)
        except FileNotFoundError as e:
            print(f"WARN: {e}", file=sys.stderr)
            continue
        out_path = MAP_DIR / f"{shard}.yaml"
        existing = read_existing_map(out_path)
        merged = merge_into_existing(generated, existing)
        n_files = len(merged["files"])
        n_syms = len(merged["symbols"])
        n_members = sum(len(s.get("members", [])) for s in merged["symbols"])
        n_missing = sum(
            1
            for s in merged["symbols"]
            for m in s.get("members", [])
            if m.get("relation") == "missing"
        )
        action = "DRY-RUN" if args.dry_run else "WROTE"
        print(
            f"{action} {out_path.relative_to(REPO_ROOT)} "
            f"({n_files} files, {n_syms} symbols, {n_members} members, "
            f"{n_missing} missing)"
        )
        if not args.dry_run:
            write_yaml(out_path, merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
