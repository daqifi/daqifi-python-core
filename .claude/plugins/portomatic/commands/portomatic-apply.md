---
description: Execute the latest apply-mode plan with reflection-loop verification per step.
allowed-tools: Bash, Read, Write, Edit, Task, AskUserQuestion
---

You are running **portomatic apply** in the downstream repo.

Goal: walk the latest apply-mode plan step by step, dispatching a Patch Author worker per step. Each worker has a reflection loop (write → verify → revise) bounded at N=3. Update plan step status as you go.

## Steps

1. List plans and pick the most recent apply-mode plan:
   ```
   portomatic plan list
   portomatic plan show latest
   ```
2. Confirm scope with `AskUserQuestion`:
   - Run all steps unattended
   - Pause for review after each step
   - Stage edits as a single PR-style commit at the end
3. **For each step (strictly serial in v0.1):**
   - Mark in_progress:
     ```
     portomatic plan step <plan-id> <step-id> --status in_progress
     ```
   - Spawn a `patch-author` subagent with the step context (`upstream_ref`, `downstream_target`, the `shard` field for map context, verifier policy from `project.yaml.verifiers.required`).
   - Wait for the worker. The worker is responsible for its own reflection loop and reports a final status (`done` / `failed`) plus a triage line.
   - Persist the result:
     ```
     portomatic plan step <plan-id> <step-id> --status <done|failed|skipped> [--note "..."] [--failure-class compile|behavioral_drift|hardware_unavailable|flaky]
     ```
   - On `failed` in unattended mode, escalate via `AskUserQuestion` (skip / retry with hint / abort).
   - On `done`, bump `last_synced_sha` for the touched shard:
     ```
     portomatic map sync-bump <shard> <upstream-sha-from-plan>
     ```
   - On `done`, if the step has `ticket_number`, close the issue (read `tickets.hub_repo` from project.yaml):
     ```
     gh issue close <ticket_number> --repo <hub_repo> --comment "Closed by portomatic apply (plan <plan-id> step <step-id>)"
     ```
4. After the last step, write a summary to `.portomatic/reports/<ts>-apply.md` and propose `/portomatic-verify` if the verifier policy requires post-apply sweeps.

## Reminder

Do not run multiple Patch Author workers in parallel in v0.1; map shards are not yet locked.
