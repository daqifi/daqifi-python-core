---
name: portomatic-overview
description: Reference for portomatic — agentic codebase porting framework. Loads when the user mentions porting an upstream codebase to a downstream language/platform, syncing a port with its upstream, or runs any /portomatic-* command.
---

# portomatic — at a glance

A framework for keeping a downstream port aligned with a moving upstream codebase. Three modes:

- **init** — set up the porting relationship
- **check** + **plan** + **apply** — absorb upstream changes into downstream
- **verify** — differential tests + builds + optional HIL

## Core artifacts (always under `.portomatic/`)

- `project.yaml` — upstream/downstream paths, languages, arch mode, adapters, verifier policy
- `sync_state.yaml` — last_synced_upstream_sha
- `map/<shard>.yaml` — correspondence map shards (one per upstream top-level module, plus `HAL.yaml` for firmware)
- `plans/<ts>-<slug>.yaml` — durable resumable plans
- `reports/<ts>-*.md` — human-readable change reports
- `cache/upstream_ir/<sha>/` and `cache/specs/`, `cache/prompts/`

## Slash commands

| Command | What it does |
|---|---|
| `/portomatic-init` | Create `.portomatic/project.yaml` after asking about arch mode, adapters, verifiers |
| `/portomatic-map` | Build/refresh correspondence map by extracting both trees and pairing symbols |
| `/portomatic-check` | Diff upstream HEAD vs `last_synced_sha`, classify each change |
| `/portomatic-plan` | Turn the change report into a per-change downstream patch plan |
| `/portomatic-apply` | Execute the plan; reflection loop per step |
| `/portomatic-verify` | Run verifier policy standalone |
| `/portomatic-status` | Print drift dashboard |

## CLI under the hood

The `portomatic` Python CLI does the work; subagents shell out to it. Key entrypoints:

- `portomatic init --upstream <path> --upstream-lang csharp --downstream-lang python --arch free`
- `portomatic adapter extract --lang <csharp|python|c> --path <file-or-dir>`
- `portomatic adapter build --kind <harmony_pic32|platformio_esp32> --path <repo> --env <env>`
- `portomatic harness run --verifier <name> --path <path>`
- `portomatic differential lift --file <xunit.cs>`
- `portomatic differential gen --spec <id> --downstream-qualified <q> --downstream-module <m> --downstream-symbol <s>`
- `portomatic differential compare --subject <name> --upstream-path <csproj> --downstream-path <pytest-dir>`
- `portomatic status`

## Architectural choices to remember

- The **downstream** repo is the workspace. Upstream is read-only (only `git log`/`git show`).
- The Architect subagent owns the mode state machine and is the only agent that asks the user questions.
- Generated differential tests live at `<downstream>/tests/portomatic-generated/` and are committed.
- `differential gen` emits a prompt file plus a skeleton; the calling Claude Code session is responsible for filling in the test body (LLM auth lives with the session, not the CLI).
- Map shards are sharded per upstream top-level dir, with concept-level `HAL.yaml` for firmware.
- HIL detection uses `PORTOMATIC_HIL_PORT`; missing device → skipped, not failed (unless `hil.required: true`).
