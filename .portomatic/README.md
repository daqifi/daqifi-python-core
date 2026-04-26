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
├── map/                   ← method-level shard maps (THIS IS THE SOURCE OF TRUTH)
│   ├── Channel.yaml
│   ├── Communication.yaml
│   ├── Communication.Consumers.yaml
│   ├── Communication.Messages.yaml
│   ├── Communication.Producers.yaml
│   ├── Communication.Transport.yaml
│   ├── Device.yaml
│   ├── Discovery.yaml
│   ├── Network.yaml
│   ├── Protocol.yaml
│   ├── SdCard.yaml
│   └── Firmware.yaml
├── cache/                 ← portomatic tool internal cache
├── reports/               ← per-run reports from past portomatic invocations
└── research/              ← scratch notes from one-off investigations
```

## Workflow

The workflow is **map-first**. The shard maps under `map/` are the
source of truth for "is this port complete." Plans are derived
artifacts that should converge on zero outstanding gaps as the port
catches up to upstream.

### 1. Regenerate the shard maps after an upstream sync

```bash
python scripts/portomatic_generate_map.py --all
```

Walks both the upstream C# tree and this repo's `daqifi/` package,
populating method-level `members:` rows for every public class. Manual
edits to `notes:` / `rationale:` / `divergences:` / `backlog:` /
`hal_pairs:` are preserved across regeneration — only the
auto-discoverable structure (file paths, symbol names, method
presence) is overwritten.

To regenerate one shard:

```bash
python scripts/portomatic_generate_map.py --shard Network
```

### 2. Audit for drift before merging or releasing

```bash
python scripts/portomatic_audit.py
```

Cross-references the maps against the codebase and reports drift in
five categories:

| Category | Meaning | Action |
|---|---|---|
| `protocol-no-impl` | Protocol declared, no concrete class implements all required methods with non-stub bodies | Implement the Protocol on a concrete class, or mark divergent |
| `method-missing` | Map says `relation: ported` but the downstream method doesn't exist | Either add the method or update the map (likely a rename) |
| `method-undocumented` | Public method exists in code, no map row | Add the row to the shard map |
| `symbol-undocumented` | Public class exists, no map entry | Map it (or move under `_internal/` if it's not part of the public surface) |
| `stale-divergent` | `relation: divergent` without rationale | Re-evaluate; either remove the divergence or refresh the rationale |

Filter by category:

```bash
python scripts/portomatic_audit.py --category protocol-no-impl
```

Strict mode for CI gates (exits 1 if any drift):

```bash
python scripts/portomatic_audit.py --strict
```

JSON output for tooling:

```bash
python scripts/portomatic_audit.py --json
```

### 3. Use the audit as the "port complete" gate

The `port complete` check is no longer "all plan files are marked
done" — it's "the audit returns zero `protocol-no-impl` and zero
`method-missing` rows." Run the audit before declaring the port
complete; treat any output as a TODO list.

`symbol-undocumented` and `method-undocumented` are softer signals
— they may indicate that the map needs to be updated to reflect a
deliberate Python-only addition. Operator decides whether to map or
ignore.

## Bootstrapping a new shard

The `SHARDS` dict in `scripts/portomatic_generate_map.py` lists every
upstream shard the generator knows about. To add a new one:

1. Edit `SHARDS` in `scripts/portomatic_generate_map.py` to add
   `"<ShardName>": "<upstream/relative/path>"`.
2. Run `python scripts/portomatic_generate_map.py --shard <ShardName>`.
3. Review the generated `.portomatic/map/<ShardName>.yaml` — fill in
   `rationale:` for any rows that need explanation, and curate the
   `divergences:` / `backlog:` lists if appropriate.
4. Re-run `python scripts/portomatic_audit.py` to confirm the new
   shard's coverage.

## Related portomatic enhancements (tracked upstream)

The following gaps in the portomatic tool itself are tracked at
[cptkoolbeenz/portomatic](https://github.com/cptkoolbeenz/portomatic/issues):

- `#147` — method-level shard map generation (foundation; the
  `scripts/portomatic_generate_map.py` script in this repo is the
  pilot implementation)
- `#148` — audit script (the `scripts/portomatic_audit.py` script
  in this repo is the pilot)
- `#149` — drift detection via `last_synced_sha`
- `#150` — derive plans from map gaps (workflow inversion)
- `#151` — plan-state reconciliation
- `#152` — coverage metric overhaul (replace plans-applied % with
  member-level coverage)
- `#153` — meta-ticket cross-referencing the batch + recommended
  sequencing

Both `scripts/portomatic_generate_map.py` and
`scripts/portomatic_audit.py` are intentionally written as
self-contained scripts in this repo so we can iterate locally before
the equivalent functionality lands in the portomatic tool itself.
Once the upstream tickets ship, these scripts will be replaced by
`portomatic map generate` and `portomatic audit` invocations.
