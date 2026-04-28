---
description: Promote the latest /portomatic-check plan into a refined apply plan.
allowed-tools: Bash, Read, Write, Edit, AskUserQuestion
---

You are running **portomatic plan** in the downstream repo.

Goal: take the latest check-mode plan (created by `/portomatic-check`), confirm scope per classification with the user, and produce an apply plan ready for `/portomatic-apply`.

## Steps

1. Identify the latest check plan:
   ```
   portomatic plan list
   portomatic plan show latest
   ```
2. Group steps by `classification` and present a count to the user via `AskUserQuestion`. Default include set:
   - **always include**: `semantic`, `api-shape`
   - **ask**: `new-feature` (port now / file as backlog)
   - **always exclude**: `cosmetic`, `irrelevant`, `test-only`, `refactor-only` (each has its own handling: lift via differential, defer, or skip)
3. Promote with the user's chosen include set:
   ```
   portomatic plan promote <check-plan-id> --include semantic,api-shape[,new-feature]
   ```
   This writes `.portomatic/plans/<ts>-apply-<sha>.yaml` containing only the chosen steps, each with `kind: patch`, `assigned_to: patch-author`, status `pending`. The original check plan is preserved.
4. For *deferred* `new-feature` items (those NOT included), add `BacklogItem` entries to the relevant map shard via Edit instead of plan steps.
5. Validate the apply plan compiles:
   ```
   portomatic plan show latest
   ```
6. **If `project.yaml.tickets.enabled` is true**, file one GitHub issue per step so the work shows up in the team's hub:
   ```
   portomatic tickets create latest
   ```
   Each step gets `ticket_url` + `ticket_number` written into the plan.
7. Propose `/portomatic-apply` (or `/portomatic-cycle` for the full chain).
