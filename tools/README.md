# Port-helper micro-tools

Tiny scripts collected during the daqifi-core → daqifi-python-core port.
Each tool exists because the same lookup or transformation happened
multiple times in a row. The goal: next port session calls these
directly instead of re-invoking ad-hoc bash + python one-liners.

| Tool | What it does |
| --- | --- |
| `find_upstream.sh` | Locate a C# source file in the upstream repo by class/interface name. |
| `coverage_status.py` | Print the portomatic-coverage record for one or more upstream symbol names (file, status, downstream match, overlap). |
| `find_downstream.sh` | Locate a Python class/Protocol/function in the downstream repo by name. |

## Conventions

- All tools assume the standard sibling-repo layout:
  - `/mnt/c/Users/User/Documents/GitHub/daqifi-core` (upstream C#)
  - `/mnt/c/Users/User/Documents/GitHub/daqifi-python-core` (this repo)
  - `/mnt/c/Users/User/Documents/GitHub/portomatic` (the porting tool)
- Tools take symbol names exactly as upstream PascalCase (`IDevice`,
  `ScpiMessageProducer`, etc.).
- Output is line-oriented and scrape-friendly so later tools can pipe.

## Adding new tools

When a port-helper one-liner shows up for the third time, promote it:

1. Drop a script (`.sh` or `.py`) in this directory.
2. Add a row to the table above with one sentence on what it does.
3. Make it idempotent and quiet on success.

See `portomatic-improvement-log.md` (sibling file) for the running list of
*portomatic itself* improvements found while porting — those become PRs
upstream of these tools.
