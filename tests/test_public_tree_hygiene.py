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

#: 16-hex-character serials. TWO rules, each decided by the TOKEN'S OWN SHAPE.
#:
#: ⛔ FOUR AUDIT ROUNDS LANDED ON ONE REGEX, AND THE DESIGN WAS THE PROBLEM.
#: Every round I adjusted a lookaround -- the characters AROUND the token -- to
#: separate "hex serial" from "float". Context cannot make that distinction,
#: and each adjustment bought one case and sold another:
#:
#:   r1  a ``0x``-prefixed serial escaped, because ``\b`` finds no boundary
#:       between the ``x`` and a hex digit
#:   r2  dropping the letter requirement to fix r1 let the rule fire on the
#:       fractional digits of a float
#:   r3  excluding decimal points to fix r2 then (a) EXEMPTED a lettered
#:       serial sitting beside a dot -- ``run1.<serial>.json``, a real leak --
#:       and (b) still failed Python's ``.5``-style and ``5.``-style floats,
#:       because the exclusion demanded a digit on BOTH sides of the dot
#:
#: The token's own shape decides it instead, and then no lookaround has to
#: guess:
#:
#: * A TOKEN CONTAINING A LETTER CANNOT BE A FLOAT. So the lettered rule has
#:   NO dot exclusions at all, and a serial beside a dot is caught again.
#: * AN ALL-DIGIT TOKEN IS ONLY UNAMBIGUOUS WITH AN EXPLICIT ``0x``. The
#:   prefix is itself what rules out a float, so that rule requires it.
#: * A BARE ALL-DIGIT RUN stays with BIG_INT_RE as a decimal, whose float
#:   exclusions are unchanged and correct for exactly that ambiguous case.
#:
#: ⚠ KNOWN LIMIT, inherent rather than unfixed: a bare all-digit 16-digit
#: serial written adjacent to a dot is genuinely indistinguishable from a
#: float, so BIG_INT_RE does not flag it. Nothing can separate those two
#: without the ``0x`` the second rule asks for. Said out loud because the
#: alternative is a rule that pretends otherwise, which is what the last
#: three rounds were.
#:
#: The capture group excludes the ``0x`` prefix in both rules, so one
#: allowlist serves every spelling: the bare and prefixed forms of a value
#: resolve to the same token.
#:
#: ⚠ Describe a serial's SHAPE in these comments; never spell one out. Doing
#: that tripped this gate on its own file four separate times -- correctly,
#: since there are no file exemptions.
HEX_SN_RE = re.compile(
    r"(?<![0-9A-Za-z_])"                       # not inside a longer token
    r"(?:0[xX])?"                              # optional C-style prefix
    r"(?=[0-9A-Fa-f]{16}(?![0-9A-Fa-f]))"      # exactly sixteen, no more
    r"(?=[0-9A-Fa-f]{0,15}[A-Fa-f])"           # at least one LETTER: not a float
    r"([0-9A-Fa-f]{16})"
)

#: An all-digit 16-character token is only unambiguously hex when it carries
#: the prefix; without it, it is a decimal and BIG_INT_RE owns it.
HEX_0X_DIGITS_RE = re.compile(
    r"(?<![0-9A-Za-z_])0[xX]([0-9]{16})(?![0-9A-Fa-f])"
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
    ("0x hex serial", HEX_0X_DIGITS_RE, ALLOWED_HEX_SNS),
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
        # all-digit hex WITH the prefix: its own rule, because the prefix is
        # what makes the token unreadable as a float
        ("hex serial, all digits, 0x", 'sn = 0x' + "98765432" + "10987654"),
        ("hex serial, all digits, 0X", 'sn = 0X' + "98765432" + "10987654"),
        # ...and bare, where it is a decimal and BIG_INT_RE owns it
        ("all digits, bare", 'sn = ' + "98765432" + "10987654"),
        # ⛔ r3 FINDING 1, A REAL LEAK: a LETTERED serial beside a dot. The
        # decimal-point exclusion exempted it, because the exclusion looked
        # at the characters around the token instead of the token itself. A
        # token containing a letter cannot be a float, so dots are irrelevant
        # to it -- which is the whole reason the design changed.
        ("lettered serial, dot before",
         "captures/run1." + "DEADBEEF" + "CAFE1234" + ".json"),
        ("lettered serial, dot after",
         "DEADBEEF" + "CAFE1234" + ".20261103.json"),
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
        # ⛔ r3 FINDING 2: Python's two one-sided float spellings. The
        # decimal-point exclusion demanded a digit on BOTH sides of the dot,
        # so neither of these was excluded and both failed the gate.
        ("float, no integer part", "scale = .2345678901234567"),
        ("float, no fractional part", "scale = 2345678901234567."),
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


def test_a_dot_beside_a_lettered_serial_never_excuses_it():
    """A dot touching a LETTERED serial is irrelevant, on either side.

    This is the property the r3 redesign bought, and it is pinned in every
    position because three successive versions of the rule each got one
    position right and another wrong. A token with a letter in it cannot be a
    float, so no dot anywhere near it can make it one -- and the lettered
    rule accordingly carries no dot exclusions to get wrong.
    """
    L = "DEADBEEF" + "CAFE1234"
    for position, text in (
            ("ending a sentence", "The unit is %s." % L),
            ("after a dotted key", "config.%s" % L),
            ("a filename stem", "dump.%s.json" % L),
            ("between two dots", "captures/run1.%s.2026.json" % L),
            ("dot then digits", "%s.20261103.json" % L),
            ("alone on a line", L),
    ):
        assert violations_in(text), position


def test_each_shape_is_owned_by_the_rule_that_should_own_it():
    """WHICH rule fires, not merely that one does.

    ⛔ A mutant that unregistered the 0x-all-digit rule SURVIVED every control
    above, because the decimal rule still catches ``0x`` + sixteen digits --
    its lookbehind excludes hex characters and ``x`` is not one. That is the
    very accident the r3 redesign set out to replace with something
    deliberate, so "some rule caught it" is too weak a claim here: the
    coverage has to belong to the rule whose job it is, or widening the
    decimal rule's character class silently reopens the hole.

    The general controls stay rule-agnostic on purpose. This one is about
    ownership, which is a different question.
    """
    L = "DEADBEEF" + "CAFE1234"
    D = "98765432" + "10987654"
    owners = {
        "lettered, bare":  ("hex serial", "sn: %s" % L),
        "lettered, 0x":    ("hex serial", "sn: 0x%s" % L),
        "all-digit, 0x":   ("0x hex serial", "sn: 0x%s" % D),
        "all-digit, 0X":   ("0x hex serial", "sn: 0X%s" % D),
        "all-digit, bare": ("long decimal serial", "sn: %s" % D),
    }
    for label, (want_rule, text) in owners.items():
        rules = {rule for _rel, _ln, rule, _tok in violations_in(text)}
        assert want_rule in rules, f"{label}: {want_rule!r} did not fire ({rules})"


def test_scan_actually_reads_files():
    """A canary: the scan must see a non-trivial tree, not an empty list."""
    assert len(_tracked_files()) > 50
