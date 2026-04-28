---
description: Register and acquire external reference docs (datasheets, errata, vendor refs) for the project.
allowed-tools: Bash, Read, WebFetch, AskUserQuestion, Task
---

You are running **portomatic research** in the downstream repo.

Goal: maintain `.portomatic/research/index.yaml` plus a local cache under `.portomatic/research/<topic>/`. The CLI is the index manager; YOU acquire content via `Read` (for local files including PDFs) or `WebFetch` (for URLs) and pipe it into the cache via `portomatic research save`.

## Steps

1. Ask the user what to register or what topic to refresh. Examples:
   - "PIC32MZ ADC peripheral docs" → topic `pic32mz_adc`
   - "ESP32-S3 ADC oneshot driver" → topic `esp32_s3_idf_adc`
   - "Microchip silicon errata for PIC32MZ" → topic `pic32mz_errata`
2. **Register** each doc with the CLI. URI may be a URL, a `file://` path, or a plain local path:
   ```
   portomatic research add \
     --topic <topic> \
     --title "<human title>" \
     --uri <url-or-path> \
     [--notes "<short rationale>"]
   ```
3. **Acquire content.** For each registered doc that isn't yet cached:
   - **URL** → use `WebFetch` with a focused prompt: "extract sections relevant to <concept>; preserve register names and bit field tables verbatim."
   - **Local file (PDF or text)** → use `Read` with the absolute path.
   - For PDFs, prefer extracting specific page ranges (use `Read` with `pages: "1-20"`) rather than the whole doc.
4. **Cache** the acquired content via stdin:
   ```
   portomatic research save --topic <topic> --slug <slug> <<'EOF'
   <content>
   EOF
   ```
   (or `--from-file <path>` if it's already on disk).
5. **List + grep** to confirm:
   ```
   portomatic research list --topic <topic>
   portomatic research grep --topic <topic> "<pattern>"
   ```

## When to use research

The Patch Author and Mapper agents will `portomatic research grep` during apply for:
- MCU peripheral bit fields (datasheet)
- Silicon errata workarounds
- Vendor SDK function signatures (Harmony, ESP-IDF)
- Protocol specs (SCPI commands, IEEE 488.2)

Cache enough that a downstream agent can answer "how does X register work" without re-fetching.

## Hard limits

- Don't acquire content the user didn't authorize. Ask before WebFetch'ing a paywalled or third-party doc.
- Don't store secrets or credentials. The cache is a flat markdown file you wrote — assume it'll be reviewed by humans.
- Don't strip register tables, bit-field labels, or numeric defaults during summarization. Those are exactly what the Patch Author needs.
