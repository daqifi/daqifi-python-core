#!/usr/bin/env bash
# Locate a Python class/Protocol/function in daqifi/ by name.
#
# Usage:  tools/find_downstream.sh ScpiMessage UdpTransport
#         tools/find_downstream.sh --include-tests StreamingDevice
#
# Prints `path:line:declaration` for matching definitions.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"

include_tests=0
if [[ "${1:-}" == "--include-tests" ]]; then
    include_tests=1
    shift
fi

if [[ $# -eq 0 ]]; then
    echo "usage: $0 [--include-tests] SYMBOL [SYMBOL...]" >&2
    exit 2
fi

scan_paths=("$repo_root/daqifi")
if [[ $include_tests -eq 1 ]]; then
    scan_paths+=("$repo_root/tests")
fi

for sym in "$@"; do
    # Match: `class X`, `class X(...)`, `def x_y(`, or module-level
    # `X =` (type aliases).
    grep -rn -E "^(class[[:space:]]+${sym}\b|def[[:space:]]+${sym}\b|${sym}[[:space:]]*=)" \
        --include='*.py' "${scan_paths[@]}" 2>/dev/null \
        | grep -v __pycache__ \
        || echo "${sym}: not found" >&2
done
