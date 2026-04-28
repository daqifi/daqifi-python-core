---
description: Build or refresh the correspondence map between upstream and downstream symbols.
allowed-tools: Bash, Read, Write, Edit, Task, AskUserQuestion
---

You are running **portomatic map** in the downstream repo.

Goal: extract both upstream and downstream trees into IR, propose symbol pairings, and write/update `.portomatic/map/*.yaml` shards.

## Steps

1. Read `.portomatic/project.yaml` for `upstream_root`, `downstream_root`, languages, arch mode.
2. Determine shard scope. For an existing project, ask whether to refresh **all** shards or a specific list (e.g. `Channel,Communication`).
3. **For each shard**, run the one-shot extract+suggest+merge:
   ```
   portomatic map autosuggest \
     --shard <Name> \
     --upstream-subdir <relative-path-from-upstream-root> \
     [--downstream-subdir <relative-path-from-downstream-root>] \
     --apply
   ```
   This:
   - reads project.yaml for paths and languages
   - resolves upstream HEAD sha
   - extracts upstream into `.portomatic/cache/upstream_ir/<sha>/<shard>.json`
   - extracts downstream ephemerally
   - calls `propose()` (which uses exact-name + token-based matching, including split/subset/superset detection)
   - merges into `.portomatic/map/<shard>.yaml` idempotently — your manual edits survive

   The summary printed will look like:
   ```
   merged shard .portomatic/map/Channel.yaml  (sha=e8d84af01032ac21602d7ef51d51485ff0c043c0)
     not_yet_ported: 7
     split: 1
     superset: 1
     subset: 1
   ```
4. **Refine ambiguities** by reading the shard YAML directly. For each `not_yet_ported`, `Grep` the downstream tree for plausible counterparts. For each auto-emitted `divergence` (kind=`split` or partial-overlap behavioral), confirm the rationale.
5. Surface remaining ambiguities to the user via `AskUserQuestion` only when more than one downstream candidate exists or the relation is non-obvious.
6. Validate with `portomatic map validate`.

## Notes on the matcher

- **Token matching** handles `AnalogChannel ↔ AnalogInputChannel` automatically as `subset` (downstream is more specific).
- **Split detection**: when upstream tokens are a strict subset of *multiple* downstream candidates, suggest emits `relation: split` with a `Divergence` entry naming all candidates.
- **Threshold**: 0.5 token-set overlap. Single-token names (e.g. `Voltage`) won't match cross-class to avoid false positives.
- The matcher does NOT do member-level token matching in v0.1; member pairings rely on exact name match.

## Architecture-mode behavior

- `arch: preserve` — propose 1:1 file mappings; flag any flatten/split as a divergence (the matcher already does this for `split`).
- `arch: free` — accept large divergences without flagging; record rationale on the divergence entry.

## After completing

Propose `/portomatic-status` to show shard counts, backlog, and not_yet_ported totals.
