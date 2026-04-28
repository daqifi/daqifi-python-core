---
description: Initialize a portomatic porting project in this repo (downstream).
allowed-tools: Bash, Read, Write, AskUserQuestion
argument-hint: "[upstream_path]"
---

You are running the **portomatic init** command in the current downstream repo.

Goal: produce a valid `.portomatic/project.yaml` and an empty `sync_state.yaml`, then call `/portomatic-map` to seed the correspondence map.

## Steps

1. **Confirm the upstream repo path.** If the user passed `$ARGUMENTS`, treat it as the upstream path. Otherwise look for sibling repos (the user often keeps related repos as siblings under the same parent directory) and propose one. Confirm the choice.
2. **Detect upstream and downstream languages.** Look at file extensions / project files (`*.csproj`, `setup.py`, `pyproject.toml`, `*.X/nbproject/`, `platformio.ini`) and propose a `--upstream-lang` and `--downstream-lang` from `csharp | python | c | cpp`.
3. **Ask the architecture mode** with `AskUserQuestion`:
   - `preserve` — mirror the upstream tree 1:1
   - `free` — downstream is idiomatic (Pythonic / C++-native), correspondence map carries the alignment
   The `free` answer is the default for C# → Python ports because Python idioms differ deeply from C#.
4. **Pick adapters.** For C# → Python use `csharp,python`. For PIC32→ESP32 firmware use `harmony_pic32,platformio_esp32`. Confirm the list.
5. **Verifier policy.** Ask which verifiers must be green to mark a step "applied". Defaults:
   - C# → Python: required = `csharp_test`, `python_test`
   - Firmware ports: required = `<source-build>`, `<target-build>`; optional = `hil_scpi`
6. **Run the CLI:**
   ```
   portomatic init --upstream <path> \
     --upstream-lang <lang> --downstream-lang <lang> \
     --arch <mode> --adapters <a,b,c>
   ```
   If the project already exists, ask before passing `--force`.
7. **After init succeeds**, propose running `/portomatic-map` to seed the correspondence map.

## Error handling

- If the upstream path doesn't exist, ask for a corrected path. Do not invent.
- If `portomatic` is not installed, instruct the user to run `pip install -e /path/to/portomatic` (or the published package once available).
- If `.portomatic/project.yaml` exists, default to NOT overwriting; ask the user explicitly.

## Reminder

You are the Architect agent for this command. Workers don't ask the user — you do.
