---
description: Run the full portomatic cycle — compare → file tickets → code → test — for one upstream window.
allowed-tools: Bash, Read, Edit, Write, Task, AskUserQuestion
---

You are running **portomatic cycle** in the downstream repo. This is the top-level loop: one invocation absorbs one upstream window of changes end-to-end.

## The cycle

```
       ┌─────────┐
       │  CHECK  │  diff upstream HEAD vs last-synced; classify each touch
       └────┬────┘
            │
            ▼
       ┌─────────┐
       │ PROMOTE │  filter to actionable; emit apply plan
       └────┬────┘
            │
            ▼
       ┌─────────┐
       │ TICKETS │  one GitHub issue per actionable step (the work hub)
       └────┬────┘
            │
            ▼
       ┌─────────┐
       │  CODE   │  Patch Author per step, reflection loop
       └────┬────┘
            │
            ▼
       ┌─────────┐
       │  TEST   │  verifier policy; bump sync state on full green
       └─────────┘
```

## Steps

1. **Check** — `portomatic check`. Read the report and tell the user the count breakdown. If `actionable_count == 0`, stop here with a "you're in sync" message.
2. **Promote** — ask the user (`AskUserQuestion`) which classifications to include. Default include set: `semantic`, `api-shape`. Ask separately about `new-feature` (port now / file as backlog). Run `portomatic plan promote <check-plan-id> --include <list>`.
3. **Tickets** — if `project.yaml.tickets.enabled` is true, run `portomatic tickets create latest [--include <list>]` to file one GitHub issue per step. Each issue title follows `tickets.title_pattern` and gets labels `portomatic`, `<classification>`, `shard:<name>`. If milestone_pattern is set, all tickets are grouped under e.g. `upstream-<sha7>`.
4. **Code** — for each pending step, dispatch the `patch-author` subagent (see `/portomatic-apply`). After the worker completes:
   - `portomatic plan step <plan-id> <step-id> --status done` (or failed)
   - If a ticket exists for the step, close it: `gh issue close <ticket_number> --repo <hub_repo> --comment "Done by portomatic apply step <step-id>"`
5. **Test** — `portomatic verify` runs the project's required verifiers across the whole touched scope as a final sweep.
6. **Bump** — if all `verifiers.required` are green and all `actionable` steps are `done`/`skipped`, bump `last_synced_upstream_sha` for each touched shard via `portomatic map sync-bump <shard> <sha>`, then update `.portomatic/sync_state.yaml.last_synced_upstream_sha`.
7. **Report** — write a one-page summary to `.portomatic/reports/<ts>-cycle.md` with: counts, ticket links, verifier results, new last_synced sha.

## Stop conditions (fail-safe defaults)

- If `actionable_count == 0` after check, report "in sync" and exit.
- If any patch-author step fails after 3 reflection iterations, mark step failed and ask the user via `AskUserQuestion` whether to continue with remaining steps.
- If any `verifiers.required` fails at the final sweep, do NOT bump `sync_state`. Tell the user which verifier failed and where.

## Notes

- The cycle never auto-merges or pushes. Code is committed only if the user runs `git commit` themselves (or the Patch Author is configured to do so per project policy).
- Tickets remain the audit trail. Closed tickets correspond to done steps; open tickets correspond to pending or failed work.
- For pure dry-runs, run `portomatic check` then `portomatic plan promote ... --include semantic,api-shape` then `portomatic tickets create --dry-run` and inspect outputs without filing anything.
