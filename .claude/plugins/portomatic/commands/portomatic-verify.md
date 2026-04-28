---
description: Run the project's verifier policy standalone (no apply).
allowed-tools: Bash, Read, Write, Task
---

You are running **portomatic verify** in the downstream repo.

Goal: invoke each `verifiers.required` (and optionally `verifiers.optional`) verifier and write a structured report.

## Steps

1. Read `project.yaml.verifiers`.
2. Ask the user (one short question) whether to run `required` only or `required + optional`.
3. For each verifier:
   ```
   portomatic harness run --verifier <name> --path <appropriate-path> [--env <env>] [--filter <expr>]
   ```
   Capture each `VerifierRun` JSON.
4. Spawn the `verifier` subagent to triage failures (`compile` / `behavioral_drift` / `flaky` / `hardware_unavailable`) and propose next actions.
5. Write `.portomatic/reports/<ts>-verify.md` summarizing all runs.

## Notes

- HIL: if the user has not set `PORTOMATIC_HIL_PORT`, `hil_scpi` reports `skipped`. Treat as informational unless `project.yaml.hil.required: true`.
- `csharp_test` requires `dotnet` on PATH. `python_test` uses pytest from the active venv. State the requirement plainly if either is missing.
