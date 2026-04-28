---
description: Diff upstream HEAD against last-synced SHA and classify every change.
allowed-tools: Bash, Read
---

You are running **portomatic check** in the downstream repo.

Goal: produce a human-readable change report at `.portomatic/reports/<ts>-change-report.md` and a machine-readable check plan at `.portomatic/plans/<ts>-check-<short-sha>.yaml`. The CLI does the git walk + heuristic classification; you summarize and propose next steps.

## Steps

1. Read `.portomatic/project.yaml` and `.portomatic/sync_state.yaml`.
2. If `sync_state.last_synced_upstream_sha` is null, ask the user (one short question) what baseline SHA to use; default to `HEAD~10` if they have no preference.
3. Run the check:
   ```
   portomatic check [--baseline <sha>]
   ```
   This walks every commit in `<baseline>..HEAD` on upstream, classifies each touched file using the correspondence map, and writes both artifacts.
4. Read the generated report and produce a short user-facing summary:
   - Counts per classification
   - The 3 most consequential `semantic` / `api-shape` rows
   - Any rows flagged "file is marked divergent — review change in light of map rationale" — these are upstream changes to files the map says are intentionally different. Worth a closer look.
5. Propose `/portomatic-plan` to refine the check plan into an apply plan.

## Classifications produced by the CLI

| label | meaning |
|---|---|
| `semantic` | upstream file is mapped; change likely needs porting |
| `new-feature` | upstream file is under a known shard root but not yet in the map |
| `test-only` | only tests touched (path-based) |
| `irrelevant` | docs/CI/build, or no shard covers this path |

Three classifications (`api-shape`, `cosmetic`, `refactor-only`) require diff-content inspection that v0.1's regex extractor doesn't do. If you suspect a row in `semantic` is actually one of those, re-classify it manually in the plan via `portomatic plan step <plan-id> <step-id> --note "...refined to api-shape..."`.

## Hard limits

- Don't auto-bump `last_synced_upstream_sha`. That happens only after `/portomatic-apply` completes successfully on a step, or via explicit `portomatic map sync-bump <shard> <sha>`.
- Don't edit downstream source in this command — `check` is read-only.
