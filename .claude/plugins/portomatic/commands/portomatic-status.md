---
description: Show the portomatic project's drift dashboard (read-only).
allowed-tools: Bash, Read
---

You are running **portomatic status** in the downstream repo.

Goal: print the drift summary. Read-only; never modifies files.

## Steps

1. Run `portomatic status`.
2. Briefly explain anything notable:
   - "last sync: (never)" → suggest `/portomatic-check` with a baseline.
   - High `not_yet_ported symbols` → suggest `/portomatic-plan` after triaging the backlog.
   - Open plan steps → name the plan ids and which steps are `pending` vs `in_progress`.
3. Do not propose actions the user did not ask for; this is a status read.
