"""WP4 root cause 2: the echo registration did not match what the console
echoes, so a leading-whitespace command left an entry nothing could match.

MEASURED on an NQ1, echo on, seven shapes:

    sent b'*IDN?\\r'      echoed b'*IDN?'     -- control
    sent b' *IDN?\\r'     echoed b'*IDN?'     -- leading space DISCARDED
    sent b'  *IDN?\\r'    echoed b'*IDN?'     -- two spaces, same
    sent b'\\t*IDN?\\r'    echoed b'*IDN?'     -- tab, same
    sent b' \\t *IDN?\\r'  echoed b'*IDN?'     -- mixed, same
    sent b'*IDN? \\r'     echoed b'*IDN? '    -- TRAILING space KEPT
    sent b'*IDN?  \\r'    echoed b'*IDN?  '   -- two trailing, kept

So the normalisation is an ``lstrip``, never a ``strip``: a symmetric one
would drop the trailing space ``get_bytes()`` really sends, which is a
defect this method's docstring already records as fixed once.

The assertions are on the CONSEQUENCE -- the echo is consumed and the
registry drains -- not on what got registered, because the registered value
is the thing being changed.
"""

import pytest

from daqifi.communication import ProtobufMessageParser


def _parser() -> ProtobufMessageParser:
    return ProtobufMessageParser(lambda data: object())


@pytest.mark.parametrize(
    "sent",
    [
        b"*IDN?\r\n",
        b" *IDN?\r\n",
        b"  *IDN?\r\n",
        b"\t*IDN?\r\n",
        b" \t *IDN?\r\n",
    ],
)
def test_a_leading_whitespace_command_still_matches_its_echo(sent) -> None:
    """The console echoes from the first non-whitespace byte whatever
    leading whitespace was sent, so every one of these must match the one
    echo the device actually produces."""
    parser = _parser()
    parser.expect_console_echo(sent)
    taken = parser._take_console_noise(b"*IDN?\r\n")
    assert taken == len(b"*IDN?\r\n"), (
        "the echo the device really sends was not recognised, so its bytes "
        "reach the frame parser as data and the next reply stalls"
    )


@pytest.mark.parametrize("sent", [b" *IDN?\r\n", b"\t*IDN?\r\n"])
def test_the_entry_drains_so_the_console_can_go_quiet(sent) -> None:
    """An unmatchable entry is not inert: the console-quiet rule needs the
    registry empty, so one immortal entry suppresses it for the life of the
    connection."""
    parser = _parser()
    parser.expect_console_echo(sent)
    parser._take_console_noise(b"*IDN?\r\n")
    assert parser._expected_echoes == [], (
        "the entry never drained, so the quiet rule cannot engage again"
    )


@pytest.mark.parametrize(
    "sent,echoed",
    [
        (b"*IDN? \r\n", b"*IDN? \r\n"),
        (b"*IDN?  \r\n", b"*IDN?  \r\n"),
    ],
)
def test_a_trailing_space_is_kept_because_the_console_echoes_it(sent, echoed) -> None:
    """⛔ THE GUARD AGAINST THE SYMMETRIC FIX. If this were a ``strip()``
    the trailing space would be dropped from the registration, the echo
    would stop matching, and the stall would come back -- which is what the
    docstring records happening once already."""
    parser = _parser()
    parser.expect_console_echo(sent)
    assert parser._take_console_noise(echoed) == len(echoed)


def test_an_all_whitespace_command_registers_nothing() -> None:
    """Nothing to echo, so nothing to wait for. Guards the lstrip against
    turning a whitespace-only write into an empty, always-matching entry.
    """
    parser = _parser()
    parser.expect_console_echo(b"  \t\r\n")
    assert parser._expected_echoes == []
