# `.portomatic/` — port state for daqifi-core → daqifi-python-core

This directory holds the portomatic state files that track the
ongoing port from `daqifi-core` (C#, upstream) to this repo
(`daqifi-python-core`, Python, downstream).

## Layout

```
.portomatic/
├── README.md              ← this file
├── project.yaml           ← top-level port config (schema, tickets, conventions)
├── sync_state.yaml        ← last-applied upstream SHA + per-shard sync state
├── plans/                 ← per-feature port plan files (kind: apply | check | port)
├── map/                   ← method-level shard maps (THE SOURCE OF TRUTH)
│   ├── Channel.yaml
│   ├── Communication.yaml
│   ├── Communication_Consumers.yaml
│   ├── Communication_Messages.yaml
│   ├── Communication_Producers.yaml
│   ├── Communication_Transport.yaml
│   ├── Device.yaml
│   ├── Discovery.yaml
│   ├── Firmware.yaml
│   ├── Logging_Export.yaml
│   ├── Network.yaml
│   ├── Protocol.yaml
│   └── SdCard.yaml
├── cache/                 ← portomatic tool internal cache
├── reports/               ← per-run reports from past portomatic invocations
└── research/              ← scratch notes from one-off investigations
```

Shard names (and so the map filenames) use letters, digits, `_` and
`-` only; portomatic refuses a dotted name such as
`Communication.Consumers`. A nested upstream namespace is written with
`_` (`Communication_Consumers`).

## Workflow

The workflow is **map-first**. The shard maps under `map/` are the
source of truth for "is this port complete." Plans are derived
artifacts that should converge on zero outstanding gaps as the port
catches up to upstream.

All commands use the **portomatic CLI** (`pip install -e
/path/to/portomatic`). The previous in-repo pilot scripts
(`scripts/portomatic_generate_map.py`, `scripts/portomatic_audit.py`)
were retired in 2026-04 once `portomatic map suggest` and
`portomatic map audit` shipped upstream — see [`docs/cli-reference.md`](https://github.com/cptkoolbeenz/portomatic/blob/main/docs/cli-reference.md)
for the full command tree.

### 1. Refresh map shards after upstream sync

```bash
# One shard at a time (interactive; review proposed pairings)
portomatic adapter extract --lang csharp \
  --path ../daqifi-core/src/Daqifi.Core/Network \
  --root ../daqifi-core --out /tmp/up.network.json

portomatic adapter extract --lang python \
  --path daqifi --root . --out /tmp/dn.python.json

portomatic map suggest \
  --shard Network \
  --upstream-root src/Daqifi.Core/Network \
  --downstream-root daqifi \
  --upstream-ir /tmp/up.network.json \
  --downstream-ir /tmp/dn.python.json \
  --apply
```

`map suggest` is idempotent — re-running on an unchanged codebase
produces no diff. Manual `notes` / `rationale` / `divergent` decisions
are preserved across regenerations.

### 2. Audit for drift before merging or releasing

```bash
# Refresh downstream IR
portomatic adapter extract --lang python --path daqifi --root . \
  --out /tmp/daqifi.ir.json

# Audit (human text)
portomatic map audit --downstream-ir /tmp/daqifi.ir.json --downstream .

# Strict mode for CI (exits 1 if any drift)
portomatic map audit --downstream-ir /tmp/daqifi.ir.json --strict

# JSON for tooling
portomatic map audit --downstream-ir /tmp/daqifi.ir.json --json

# Filter to specific drift categories
portomatic map audit --downstream-ir /tmp/daqifi.ir.json \
  --category protocol-no-impl
```

The audit reports drift in five categories:

| Category | Meaning |
|---|---|
| `protocol-no-impl` | Protocol declared, no concrete class implements all required methods |
| `method-missing` | Map says `one_to_one` but the downstream method doesn't exist |
| `method-undocumented` | Public method/property in the IR not on any map row |
| `symbol-undocumented` | Public class in the IR with no map entry |
| `stale-divergent` | `relation: divergent` without `rationale` |

### 3. Use the audit as the "port complete" gate

The "port complete" check is no longer "all plan files marked done"
— it's `portomatic map audit --strict` returning exit 0. Run before
declaring the port complete; treat any drift as a TODO list.

## Schema vocabulary

The maps use the official `portomatic.map.schema.MapShard` Pydantic
models. The relation values are:

| value | meaning |
|---|---|
| `one_to_one` | direct equivalent |
| `subset` | downstream covers part of upstream |
| `superset` | downstream covers more than upstream |
| `flattened` | several upstream artifacts collapsed into one downstream |
| `split` | one upstream artifact split into several downstream |
| `divergent` | intentional behavioral or shape divergence (rationale required) |
| `dropped` | not present downstream and won't be |
| `not_yet_ported` | not present yet but planned |
| `shared_library` | both sides depend on a vendored 3rd-party |

See [`docs/correspondence-map-schema.md`](https://github.com/cptkoolbeenz/portomatic/blob/main/docs/correspondence-map-schema.md)
in the portomatic repo for the full schema reference.

## History — the in-repo pilot

Before `portomatic map suggest` and `portomatic map audit` shipped
upstream, this repo carried two pilot scripts at
`scripts/portomatic_generate_map.py` and
`scripts/portomatic_audit.py`. The pilots used a local relation
vocabulary (`ported` / `missing` / `downstream-only`) that didn't
match the official schema, plus extra member-level fields that the
schema's `MemberMapping` doesn't allow.

In 2026-04 a one-shot migration script
(`scripts/migrate_pilot_maps.py`, since deleted) translated the
pilot vocabulary to the official schema. The migration:

* `ported` → `one_to_one`
* `missing` → `not_yet_ported`
* `downstream-only` → `divergent` (with auto-rationale "Downstream-only
  addition; no upstream counterpart"); member rows with no upstream
  counterpart were dropped (audit re-derives them as
  `method-undocumented` from the IR)
* `dataclass` / `protocol` / `record` symbol kinds → `class` /
  `interface` / `class`
* Member-level `upstream_kind` / `downstream_kind` / `rationale`
  fields stripped (no curated content was lost — verified at
  migration time)
* `schema_version: 2` → `schema_version: 1`

All 12 shards now validate against the official schema. The pilot
scripts have been deleted; `portomatic map suggest` and
`portomatic map audit` are the canonical replacements.

The lesson — parallel reimplementations of features that already
exist in portomatic are wasted effort and drift over time. Before
writing a new helper script in this repo, run `portomatic --help`
and check
[`docs/cli-reference.md`](https://github.com/cptkoolbeenz/portomatic/blob/main/docs/cli-reference.md)
in the portomatic repo first.
