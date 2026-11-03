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

#: 16-hex-character serials, in either spelling.
#:
#: ⛔ THE FIRST VERSION MISSED THE ``0x`` FORM, which CONTRIBUTING.md documents
#: as how to set the expected serial. It opened with ``\b``, and between the
#: ``x`` of a prefix and the first hex digit BOTH characters are word
#: characters -- so there is no boundary there and the pattern never engaged.
#: A 0x-prefixed sixteen-character serial sailed straight through a gate
#: written to catch exactly that.
#:
#: Two changes, so neither spelling depends on an accident:
#:
#: * an explicit optional ``0[xX]`` prefix replaces the leading ``\b``, with a
#:   lookbehind that keeps the token from matching inside a longer word;
#: * the "at least one letter" requirement is GONE. It was what forced a
#:   16-digit hex literal to fall through to BIG_INT_RE, which caught it only
#:   because ``x`` happens not to be a hex character and so slipped past that
#:   rule's lookbehind. True today, silently false the moment someone widens
#:   that character class. One rule now owns every 16-hex token.
#:
#: The capture group excludes the prefix, so a single allowlist serves both
#: spellings: ``0x0123456789ABCDEF`` and the bare form resolve to one value.
#:
#: ⛔ AND DROPPING THE LETTER REQUIREMENT COST THE FLOAT EXCLUSION IT HAD BEEN
#: PROVIDING BY ACCIDENT. Sixteen consecutive digits are valid hex, so
#: ``voltage = 1.2345678901234567`` and ``sample = 0.1234567890123457`` were
#: both reported as serials -- a FALSE POSITIVE introduced by the fix above,
#: in a rule whose previous version had no such problem. The decimal rule had
#: carried these exclusions from the start; this one inherited none of them,
#: and the float control only ever exercised the decimal rule, so nothing
#: caught it. That control now runs through the same scan every rule uses.
#:
#: The two lookarounds are deliberately NARROWER than "reject an adjacent
#: dot": they reject a dot only where it is a DECIMAL POINT -- preceded by a
#: digit, or followed by one. A serial written at the end of a sentence is
#: therefore still caught (there is a test for it), where a blanket dot
#: exclusion would have quietly excused it.
#:
#: ⚠ Writing that example out in full here is what tripped this gate on its
#: own file for the fourth time. Describe the shape; never spell one.
HEX_SN_RE = re.compile(
    r"(?<![0-9A-Za-z_])"                       # not inside a longer token
    r"(?<![0-9]\.)"                            # not a fractional part
    r"(?:0[xX])?"                              # optional C-style prefix
    r"(?=[0-9A-Fa-f]{16}(?![0-9A-Fa-f]))"      # exactly sixteen, no more
    r"([0-9A-Fa-f]{16})"
    r"(?!\.[0-9])"                             # not an integer part
)
ALLOWED_HEX_SNS = {
    # the firmware's own documented example value; tests/fixtures/README.md
    # records that every captured fixture header was rewritten to it
    "0123456789ABCDEF",
    # the synthetic in daqifi/_internal/serial_identity.py, whose own comment
    # notes it is "also the hex spelling of another valid" serial -- so it is
    # legitimate in both spellings and belongs in both allowlists
    "1234567890123456",
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


#: (rule name, pattern, allowlist). One table, so a rule added here is
#: automatically subjected to every control below -- which is the structural
#: half of the float finding: the old controls named rules individually, so a
#: rule gained after a control was written was covered by none of them.
RULES = (
    ("serial port", PORT_RE, ALLOWED_PORTS),
    ("long decimal serial", BIG_INT_RE, ALLOWED_BIG_INTS),
    ("hex serial", HEX_SN_RE, ALLOWED_HEX_SNS),
    ("identity string", IDN_RE, ALLOWED_IDN_SNS),
    ("absolute Windows path", WIN_PATH_RE, frozenset()),
    ("bench lane name", LANE_RE, frozenset()),
)


def violations_in(text, rel="<sample>"):
    """[(rel, lineno, rule, token)] for one blob of text, by EVERY rule.

    The real scan and the controls both go through here, so a control
    exercises every rule rather than the one its author had in mind.
    """
    bad = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pattern, allowed in RULES:
            for token in pattern.findall(line):
                # a pattern with no capture group yields the whole match;
                # WIN_PATH_RE has none and its match is not a token worth
                # reporting, so it reports an empty one.
                token = token if isinstance(token, str) else token[0]
                if pattern is WIN_PATH_RE:
                    bad.append((rel, lineno, name, ""))
                elif token not in allowed:
                    bad.append((rel, lineno, name, token))
    return bad


def _scan():
    """[(path, lineno, rule, token)] for every violation in the shipped tree."""
    bad = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing textual to leak
        bad.extend(violations_in(text, path.relative_to(REPO).as_posix()))
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

# ⛔ EVERY CONTROL GOES THROUGH violations_in(), NOT THROUGH ONE NAMED RULE.
# The float false positive got in because the float control tested BIG_INT_RE
# by name while the fault was in HEX_SN_RE -- a control that names its rule
# covers only the rules that existed when it was written. Routing them through
# the same scan the tree gets means a rule added to RULES is immediately
# subject to all of them, in both directions.

def test_each_rule_rejects_a_plausible_leak():
    """A leak of every shape is reported. Samples are built from fragments so
    this file contains no token its own scan would reject."""
    leaks = [
        ("bench port", "connected on " + "COM" + "7"),
        ("posix port", "port /dev/" + "ttyACM" + "4"),
        ("long decimal", "device_sn = " + "9" * 19),
        # a MADE-UP hex serial, in the 16-character shape a real one takes
        ("hex serial", "sn: " + "DEADBEEF" + "CAFE1234"),
        # the 0x spelling CONTRIBUTING.md documents, which r1 found escaping
        ("hex serial, 0x",
         'DAQIFI_EXPECTED_SERIAL_NUMBER="0x' + "DEADBEEF" + 'CAFE1234"'),
        ("hex serial, 0X",
         'DAQIFI_EXPECTED_SERIAL_NUMBER="0X' + "DEADBEEF" + 'CAFE1234"'),
        # all-digit hex: no letters to key on, so this is the shape that used
        # to reach the decimal rule only by accident
        ("hex serial, all digits", 'sn = 0x' + "98765432" + "10987654"),
        ("identity string",
         "*IDN? -> " + "DAQiFi,Nq1," + "FEEDFACE" + "12345678" + ",1.0"),
        ("host path", "saved under " + "/mnt/" + "c" + "/Users/someone"),
        ("lane name", "captured by the " + "nq" + "-a" + " bench lane"),
    ]
    for label, text in leaks:
        assert violations_in(text), f"{label}: nothing reported for {text!r}"


def test_no_rule_fires_on_what_the_tree_legitimately_contains():
    """The converse, and through the same scan: a gate that rejects
    everything is no more useful than one that rejects nothing."""
    allowed = [
        # canonical port examples, in the forms the docs and tests use
        ("posix example", "connect('/dev/ttyACM0')"),
        ("windows example", "or COM3 on Windows"),
        ("synthetic port", 'port="COM255"'),
        # ⛔ r2: FLOATS. Sixteen consecutive digits are valid hex, so these
        # were reported as serials once the letter requirement went. Both
        # shapes the audit found, and pi for good measure.
        ("float, fractional 16", "voltage = 1.2345678901234567"),
        ("float, fractional 16 b", "sample = 0.1234567890123457"),
        ("float, integer part 16", "scaled = 1234567890123457.5"),
        ("pi", "precise = 3.141592653589793"),
        # digit runs inside longer hex payloads
        ("intel-hex record", ':10000000AABBCCDDEEFF00112233445566778899F8'),
        ("protobuf vector", '"08e186ae900e400248015064880110900108ba01040000a040"'),
        ("40-char git sha", "7519918a73570613c4d16312e2926bb934d7ea0b"),
        ("17 hex characters", "x = " + "ABCDEF0123456789A"),
        # an ALLOWLISTED value stays allowed in every spelling. A rule that
        # caught `0x<allowed>` would fail the tree it protects.
        ("allowlisted, bare", 'sn="0123456789ABCDEF"'),
        ("allowlisted, 0x", 'sn="0x0123456789ABCDEF"'),
        ("allowlisted, 0X", 'sn="0X0123456789ABCDEF"'),
        ("allowlisted decimal, 0x", 'sn="0x1234567890123456"'),
    ]
    for label, text in allowed:
        found = violations_in(text)
        assert not found, f"{label}: {text!r} wrongly reported as {found}"


def test_a_dot_beside_a_serial_does_not_excuse_it():
    """The float exclusions reject a dot only where it is a DECIMAL POINT.

    BOTH directions are pinned, and the second one only exists because a
    mutant survived the first. Replacing the leading exclusion with a blanket
    "not after a dot" still passed the sentence-end case below, because that
    case exercises the TRAILING exclusion -- a control for one end of a rule
    says nothing about the other.
    """
    # trailing: a serial ending a sentence, as prose would write it
    assert violations_in("The unit is " + "DEADBEEF" + "CAFE1234.")
    # leading: a serial after a dot that is NOT a decimal point -- an
    # attribute, a dotted key, a filename stem. A blanket exclusion would
    # let a leak hide in any of them.
    assert violations_in("config." + "DEADBEEF" + "CAFE1234")
    assert violations_in("dump." + "DEADBEEF" + "CAFE1234" + ".json")


def test_scan_actually_reads_files():
    """A canary: the scan must see a non-trivial tree, not an empty list."""
    assert len(_tracked_files()) > 50
