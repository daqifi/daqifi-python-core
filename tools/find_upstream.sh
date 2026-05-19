#!/usr/bin/env bash
# Locate a C# source file in daqifi-core by symbol name.
#
# Usage:  tools/find_upstream.sh IDevice
#         tools/find_upstream.sh ScpiMessageProducer CompositeMessageParser
#
# Prints `path:line:declaration` for each definition (class/interface/struct
# /record/enum) that matches the requested name. Falls back to a filename
# match if the declaration grep finds nothing -- useful when the type is
# nested or aliased.
set -euo pipefail

UPSTREAM_ROOT="${UPSTREAM_ROOT:-/mnt/c/Users/User/Documents/GitHub/daqifi-core}"

if [[ $# -eq 0 ]]; then
    echo "usage: $0 SYMBOL [SYMBOL...]" >&2
    exit 2
fi

for sym in "$@"; do
    # Match `class X`, `interface X`, `struct X`, `record X`, `enum X`
    # at word boundaries; ignore comments by skipping lines that start with `///`.
    hits=$(grep -rn -E "^[[:space:]]*(public[[:space:]]+|internal[[:space:]]+|sealed[[:space:]]+|abstract[[:space:]]+|static[[:space:]]+|partial[[:space:]]+)*((class|interface|struct|record|enum)[[:space:]]+${sym}\b)" \
        --include='*.cs' "$UPSTREAM_ROOT/src" 2>/dev/null || true)

    if [[ -n "$hits" ]]; then
        echo "$hits"
    else
        # Fall back to filename match.
        files=$(find "$UPSTREAM_ROOT/src" -name "${sym}.cs" 2>/dev/null || true)
        if [[ -n "$files" ]]; then
            for f in $files; do
                echo "$f:1:(file match — declaration not detected)"
            done
        else
            echo "${sym}: not found" >&2
        fi
    fi
done
