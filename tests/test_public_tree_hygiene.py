"""The published tree carries no workstation-specific or private detail.

This is a release gate, not a style check. It runs over ``git ls-files`` --
the set of files that actually ship -- and fails on six shapes that have each
leaked into this repository at least once:

* a serial port that names one particular bench,
* a long decimal board serial,
* a 16-hex-character board serial (DAQiFi serials are hex, so a digits-only
  rule misses them entirely),
* a device identity string of the ``DAQiFi,Nq<n>,<serial>,...`` shape, which
  is how serials arrive in hardware-run sidecars,
* an absolute path into somebody's Windows drive,
* an internal bench-lane name.

Each rule carries an allowlist of values verified synthetic at the time it was
written, and the lists are deliberately short: a new port or serial literal
should require a visible edit here, which is the point of the gate.

**There are no file exemptions, including for this file.** A gate that skips
itself cannot catch a leak pasted into it, and a detector that matches its own
prose is a trap this codebase has hit before -- so the negative-control samples
below are assembled from fragments at run time rather than written out, and
every literal this file does contain is an allowlisted value.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- the rules

#: Serial ports. Canonical documentation examples, plus the made-up series the
#: discovery and lifecycle unit tests enumerate. A real bench port is not here,
#: which is the whole content of the rule.
ALLOWED_PORTS = (
    # POSIX and Windows examples used throughout the README, docstrings
    # and examples/
    {"ttyACM0", "ttyACM1", "COM1", "COM3", "COM9"}
    # fakes in tests/test_usb_location.py and the generated transport tests
    | {"ttyACM5", "ttyACM7", "ttyACM9"}
    # the synthetic range tests/test_discovery_network.py and
    # tests/test_device_lifecycle.py invent to populate a port listing
    | {f"COM{n}" for n in range(41, 60)}
    | {"COM255"}
)
PORT_RE = re.compile(r"\b(?:COM[0-9]+|ttyACM[0-9]+)\b")

#: Long decimal integers. The lookarounds exclude hex letters as well as
#: digits and dots, so a digit run inside an Intel-HEX record or a protobuf
#: wire vector is not a match, and neither is the fractional part of a float.
BIG_INT_RE = re.compile(r"(?<![0-9A-Fa-f.])[0-9]{15,}(?![0-9A-Fa-f.])")
ALLOWED_BIG_INTS = {
    "1234567890123456789",  # the synthetic protobuf device_sn used repo-wide
    "1234567890123456",     # synthetic example in daqifi/_internal/serial_identity
}

#: 16-hex-character serials. Requires at least one letter, so a 16-digit
#: decimal is left to BIG_INT_RE and a long git SHA does not match at all
#: (the word boundaries require exactly sixteen characters).
HEX_SN_RE = re.compile(
    r"\b(?=[0-9A-Fa-f]{16}\b)(?=[0-9A-Fa-f]*[A-Fa-f])[0-9A-Fa-f]{16}\b"
)
ALLOWED_HEX_SNS = {
    # the firmware's own documented example value; tests/fixtures/README.md
    # records that every captured fixture header was rewritten to it
    "0123456789ABCDEF",
}

#: Device identity strings, as a hardware run records them.
IDN_RE = re.compile(r"DAQiFi,[A-Za-z0-9 ]+,([0-9A-Fa-f]{8,})")
ALLOWED_IDN_SNS = ALLOWED_HEX_SNS | ALLOWED_BIG_INTS

#: No allowlist for these two: there is no legitimate reason for either.
WIN_PATH_RE = re.compile(r"/mnt/[a-z]/")
LANE_RE = re.compile(r"\bnq-[a-z]\b")


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    return [REPO / n for n in out.split("\0") if n]


def _scan():
    """[(path, lineno, rule, token)] for every violation in the shipped tree."""
    bad = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing textual to leak
        rel = path.relative_to(REPO).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            for token in PORT_RE.findall(line):
                if token not in ALLOWED_PORTS:
                    bad.append((rel, lineno, "serial port", token))
            for token in BIG_INT_RE.findall(line):
                if token not in ALLOWED_BIG_INTS:
                    bad.append((rel, lineno, "long decimal serial", token))
            for token in HEX_SN_RE.findall(line):
                if token not in ALLOWED_HEX_SNS:
                    bad.append((rel, lineno, "hex serial", token))
            for token in IDN_RE.findall(line):
                if token not in ALLOWED_IDN_SNS:
                    bad.append((rel, lineno, "identity string", token))
            if WIN_PATH_RE.search(line):
                bad.append((rel, lineno, "absolute Windows path", ""))
            for token in LANE_RE.findall(line):
                bad.append((rel, lineno, "bench lane name", token))
    return bad


def test_published_tree_has_no_private_detail():
    violations = _scan()
    assert not violations, "private detail in the published tree:\n" + "\n".join(
        f"  {p}:{n}: {rule} {tok!r}" for p, n, rule, tok in violations
    )


# ------------------------------------------------- the gate can actually fail
# Each sample is assembled from fragments so this file contains no literal that
# its own scan would reject. Without these the gate could pass by matching
# nothing, which is the failure mode a hygiene check is most prone to.

def test_each_rule_rejects_a_plausible_leak():
    samples = [
        (PORT_RE, ALLOWED_PORTS, "connected on " + "COM" + "7"),
        (PORT_RE, ALLOWED_PORTS, "port /dev/" + "ttyACM" + "4"),
        (BIG_INT_RE, ALLOWED_BIG_INTS, "device_sn = " + "9" * 19),
        # a MADE-UP hex serial, in the 16-character shape a real one takes
        (HEX_SN_RE, ALLOWED_HEX_SNS, "sn: " + "DEADBEEF" + "CAFE1234"),
        # the serial is split too: written whole it is a 16-character token
        # and the scan above rejected THIS FILE on the first run, which is
        # the no-exemption design working as intended
        (IDN_RE, ALLOWED_IDN_SNS,
         "*IDN? -> " + "DAQiFi,Nq1," + "FEEDFACE" + "12345678" + ",1.0"),
    ]
    for pattern, allowed, text in samples:
        found = [t for t in pattern.findall(text) if t not in allowed]
        assert found, f"rule did not reject {text!r}"

    assert WIN_PATH_RE.search("saved under " + "/mnt/" + "c" + "/Users/someone")
    assert LANE_RE.search("captured by the " + "nq" + "-a" + " bench lane")


def test_rules_accept_what_the_tree_legitimately_contains():
    """The converse: a gate that rejects everything is no more useful."""
    # canonical examples must not trip the port rule
    for ok in ("/dev/ttyACM0", "COM3 on Windows", "COM255"):
        assert not [t for t in PORT_RE.findall(ok) if t not in ALLOWED_PORTS]
    # a float's fractional digits are not a serial
    assert not BIG_INT_RE.findall("precise = 3.141592653589793")
    # nor is a digit run inside a hex record
    assert not BIG_INT_RE.findall(":10000000AABBCCDDEEFF00112233445566778899F8")
    # a 40-character git SHA is not a 16-character serial
    assert not HEX_SN_RE.findall("7519918a73570613c4d16312e2926bb934d7ea0b")


def test_scan_actually_reads_files():
    """A canary: the scan must see a non-trivial tree, not an empty list."""
    assert len(_tracked_files()) > 50
