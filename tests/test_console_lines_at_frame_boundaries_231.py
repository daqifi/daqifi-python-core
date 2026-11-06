"""#231 / #204 items 1-2: console lines at frame boundaries.

#204 item 1, reproduced: with echo on, the device's reply to
``SYSTem:SYSInfoPB?`` is preceded by the echoed command line.
``ProtobufMessageParser`` reads the ``'S'`` (0x53) as an 83-byte length and
the ``'Y'`` after it passes the field-tag check, so it waits for bytes that
never come while the real frame sits behind it -- ``msgs=0, consumed=0``,
forever.

#231's direction is to route by what the library KNOWS rather than guess
from bytes, and the library knows what it just sent. So the parser is told
which lines are console noise and skips them, but ONLY:

  * at a frame boundary -- never part-way into a frame, and never when the
    parser is waiting for the rest of one;
  * on an EXACT match of a complete line, line ending included.

Both limits matter: every byte-level heuristic tried in #204's two audit
rounds either ate a partial frame or starved complete text, because on a
length-prefixed protocol a printable byte is also a valid length or tag.
An exact whole-line match at a boundary is decidable; "does this look like
text" is not.

The eight #204 acceptance repros live in test_protocol_parsing_204.py and
pin that none of this skipping can eat a frame.
"""

import importlib.util
import os
import sys
import time

import pytest

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.communication import ProtobufMessageParser, StreamMessageConsumer

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes_231",
    os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py"),
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


def _scpi():
    from daqifi._internal import scpi_commands

    return scpi_commands


def _frame(message) -> bytes:
    body = message.SerializeToString()
    out = bytearray()
    n = len(body)
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + body


def _reply() -> bytes:
    return _frame(DaqifiOutMessage(device_sn=1234, device_pn="Nq1"))


def _parser(**kwargs):
    return ProtobufMessageParser(DaqifiOutMessage.FromString, **kwargs)


# ---- #204 item 1: the echoed command line --------------------------------


def test_an_echoed_command_before_a_frame_does_not_stall_the_parser() -> None:
    """The defect, exactly as #204 reports it."""
    parser = _parser()
    parser.expect_console_echo("SYSTem:SYSInfoPB?")
    data = b"SYSTem:SYSInfoPB?\r\n" + _reply()

    messages, consumed = parser.parse_messages(data)
    assert len(messages) == 1, (
        "the echoed command line stalled the parser: the frame behind it "
        "was never read"
    )
    assert messages[0].data.device_sn == 1234
    assert consumed == len(data), "the echo was not consumed with the frame"


def test_an_echo_is_skipped_only_when_the_library_sent_that_command() -> None:
    """Keyed on what was actually sent, not on looking like a command.

    A line that merely resembles SCPI is NOT skipped: that is the
    byte-level guessing #204's audit rounds ruled out, and a payload can
    begin with printable runs.
    """
    parser = _parser()
    parser.expect_console_echo("SYSTem:SYSInfoPB?")
    # A DIFFERENT command the library never sent.
    data = b"SYSTem:STReam:FORmat?\r\n" + _reply()
    messages, consumed = parser.parse_messages(data)
    assert (messages, consumed) == ([], 0), (
        "a line the library never sent was skipped as echo"
    )


def test_an_echo_is_skipped_only_as_a_whole_line() -> None:
    """A truncated echo is not a match: the rest may still be arriving."""
    parser = _parser()
    parser.expect_console_echo("SYSTem:SYSInfoPB?")
    assert parser.parse_messages(b"SYSTem:SYSInfo") == ([], 0)
    # ... and once the whole line plus a frame is there, it is skipped.
    messages, consumed = parser.parse_messages(
        b"SYSTem:SYSInfoPB?\r\n" + _reply()
    )
    assert len(messages) == 1 and consumed


def test_an_echo_is_consumed_once_and_not_for_the_next_frame() -> None:
    """Skipping is one-shot per command sent, so a payload that happens to
    contain the same bytes later is not eaten."""
    parser = _parser()
    parser.expect_console_echo("SYSTem:SYSInfoPB?")
    first = b"SYSTem:SYSInfoPB?\r\n" + _reply()
    messages, _ = parser.parse_messages(first)
    assert len(messages) == 1
    # The same line again, with no new command sent: no longer expected.
    again = b"SYSTem:SYSInfoPB?\r\n" + _reply()
    assert parser.parse_messages(again) == ([], 0), (
        "an echo was skipped for a command that was never re-sent"
    )


# ---- the prompt and the error line ---------------------------------------


def test_the_console_prompt_before_a_frame_is_skipped() -> None:
    """⭐ The one BEHAVIOURAL discriminator here that needs no new API: on
    the old parser this stalls, because ``D`` (0x44) reads as a 68-byte
    length and it waits for bytes that never come. Every other test in this
    file fails on the old parser for an API-shape reason (the methods did
    not exist), which is honest for tests of new API but is not evidence of
    the defect. The defect itself is: ``echo + frame`` gave
    ``msgs=0, consumed=0`` while the frame alone parsed -- measured on
    main's parser directly."""
    parser = _parser()
    data = b"DAQIFI> " + _reply()
    messages, consumed = parser.parse_messages(data)
    assert len(messages) == 1 and consumed == len(data)


def test_an_error_line_is_routed_to_the_sink_not_dropped() -> None:
    """``**ERROR: ...`` is console noise to the frame parser but it is the
    device reporting something; it goes to a sink rather than silently
    away."""
    seen = []
    parser = _parser(console_error_sink=seen.append)
    data = b"**ERROR: -113 Undefined header\r\n" + _reply()
    messages, consumed = parser.parse_messages(data)
    assert len(messages) == 1 and consumed == len(data)
    assert seen == ["**ERROR: -113 Undefined header"], (
        f"the error line was dropped instead of reported: {seen}"
    )


def test_an_incomplete_error_line_is_not_consumed() -> None:
    """Without its line ending the line may still be arriving, and
    consuming it would lose whatever follows."""
    seen = []
    parser = _parser(console_error_sink=seen.append)
    assert parser.parse_messages(b"**ERROR: -113 Undef") == ([], 0)
    assert seen == []


# ---- the limits that keep the eight acceptance repros true ---------------


@pytest.mark.parametrize(
    "data",
    [
        bytes.fromhex("0a2a020801"),
        (bytes.fromhex("412a3f6162630d0a05080412010a") + b"x" * 52)[:14],
    ],
    ids=["printable-length-and-tag", "A*?abc-payload"],
)
def test_console_skipping_never_eats_a_partial_frame(data) -> None:
    """The skipper runs at a frame boundary only. A partial frame whose
    payload happens to contain printable runs or a CRLF must still be held,
    which is what #204's audit rounds kept breaking."""
    parser = _parser(console_error_sink=lambda line: None)
    parser.expect_console_echo("SYSTem:SYSInfoPB?")
    assert parser.parse_messages(data) == ([], 0)


def test_a_frame_whose_payload_contains_the_prompt_is_not_truncated() -> None:
    """CONTROL -- passes on the old parser too, by design.

    The prompt inside a payload is payload, because the skipper only looks
    at a boundary. It guards against the skipper being made greedy later;
    it is not evidence for this change.
    """
    message = DaqifiOutMessage(device_sn=7, device_pn="DAQIFI> x")
    data = _frame(message)
    parser = _parser()
    messages, consumed = parser.parse_messages(data)
    assert len(messages) == 1 and consumed == len(data)
    assert messages[0].data.device_pn == "DAQIFI> x"


# ---- end to end, through the real device and consumer -------------------


def test_an_echo_on_device_delivers_its_protobuf_reply() -> None:
    """The wiring, not just the parser: a device with echo ON gets its
    reply delivered.

    This is #204 item 1 as a user sees it. The device sends
    ``SYSTem:SYSInfoPB?``, the firmware echoes the command line and then
    writes the frame, and the consumer must dispatch the frame rather than
    stall on the echo. It passes only if the device registers what it sent
    with the parser the consumer is actually using -- the parser-level
    tests above would pass with the wiring missing.
    """
    import importlib.util
    import os
    import threading
    import time

    here = os.path.dirname(__file__)
    spec = importlib.util.spec_from_file_location(
        "sd_fakes_231",
        os.path.join(here, "portomatic-generated",
                     "test_sd_card_operations_impl.py"),
    )
    fakes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fakes)

    from daqifi._internal import scpi_commands

    device = fakes._make_device_with_serial_facade()
    delivered = []
    device._consumer.add_message_received(
        lambda args: delivered.append(args.message)
    )
    try:
        stream = device._transport.stream
        command = scpi_commands.get_device_info()
        # The firmware with echo on: the command line, then the frame.
        echoed = str(command.data).encode() + b"\r\n"
        device.send(command)
        stream.feed(echoed + _reply())

        deadline = time.monotonic() + 10.0
        while not delivered and time.monotonic() < deadline:
            time.sleep(0.01)
        assert delivered, (
            "the echoed command line stalled the consumer: the frame "
            "behind it was never dispatched"
        )
        # The consumer wraps a frame in ObjectInboundMessage.
        payload = getattr(delivered[0], "data", delivered[0])
        assert getattr(payload, "device_sn", None) == 1234, (
            f"the wrong thing was dispatched: {delivered[0]!r}"
        )
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


def test_the_device_wires_the_error_sink_so_a_console_error_is_reported(caplog) -> None:
    """⭐ THE SINK MUST BE WIRED, NOT MERELY AVAILABLE.

    My first version of this change added ``console_error_sink`` and tested
    it at the parser, but the device never supplied one -- so in production
    a ``**ERROR:`` line was skipped and dropped, trading one silent failure
    for another. That is the gap this test closes: it asserts through the
    real device, where the wiring either exists or does not.
    """
    import importlib.util
    import logging
    import os
    import time

    here = os.path.dirname(__file__)
    spec = importlib.util.spec_from_file_location(
        "sd_fakes_231b",
        os.path.join(here, "portomatic-generated",
                     "test_sd_card_operations_impl.py"),
    )
    fakes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fakes)

    device = fakes._make_device_with_serial_facade()
    delivered = []
    device._consumer.add_message_received(
        lambda args: delivered.append(args.message)
    )
    try:
        with caplog.at_level(logging.WARNING, logger="daqifi.devices"):
            device._transport.stream.feed(
                b"**ERROR: -113 Undefined header\r\n" + _reply()
            )
            deadline = time.monotonic() + 10.0
            while not delivered and time.monotonic() < deadline:
                time.sleep(0.01)

        assert delivered, "the frame behind the error line was not dispatched"
        assert any("-113 Undefined header" in r.getMessage()
                   for r in caplog.records), (
            "the console error line was skipped and then dropped: the sink "
            f"is not wired. Records: {[r.getMessage() for r in caplog.records]}"
        )
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


# ---- audit round 1 on 5c724724: five findings ---------------------------


def test_a_frame_that_decodes_wins_over_the_prompt_reading() -> None:
    """r1 #1: the prompt match could destroy a real frame.

    ``44 41 51 49 46 49 3E 20`` is BOTH the prompt ``"DAQIFI> "`` and a
    68-byte frame whose body starts ``AQIFI> ``. On a length-prefixed
    stream that overlap is irreducible, so something has to win, and the
    frame does: a frame that DECODES is positive evidence, while "these
    bytes spell a prompt" is a coincidence any 69-byte payload can produce.

    The factory here accepts any bytes, which is the point -- the rule
    under test is the ORDERING, and decodability is the factory's business.
    """
    body = b"AQIFI> " + bytes(range(61))          # 68 bytes total
    assert len(body) == 68
    data = bytes([68]) + body
    parser = ProtobufMessageParser(lambda b: bytes(b))

    messages, consumed = parser.parse_messages(data)
    assert len(messages) == 1, (
        "a decodable frame was consumed as the console prompt and destroyed"
    )
    assert messages[0].data == body
    assert consumed == len(data)


def test_the_prompt_is_still_skipped_when_it_is_not_a_frame() -> None:
    """The other side of that rule: the prompt must still work. Followed by
    a real frame, the 68-byte reading does not decode, which refutes it and
    lets the console reading through."""
    parser = _parser()
    data = b"DAQIFI> " + _reply()
    messages, consumed = parser.parse_messages(data)
    assert len(messages) == 1 and consumed == len(data)


@pytest.mark.parametrize(
    "first, rest",
    [
        (b"EN", b"Able:VOLTage:DC 1,1\r\n"),
        (b"**ERROR: -1", b"13 Undefined header\r\n"),
        (b"DAQ", b"IFI> "),
    ],
    ids=["split-echo", "split-error-line", "split-prompt"],
)
def test_a_split_console_line_is_held_not_eaten(first, rest) -> None:
    """r1 #2: an INCOMPLETE console line fell through to binary parsing,
    which discarded a byte of it. An echo arriving as ``b'EN' |
    b'NAble...'`` lost the ``E`` and then stalled the next reply forever.

    A strict prefix of console noise must be HELD -- the same rule the
    frame parser already applies to a partial frame.
    """
    parser = _parser(console_error_sink=lambda line: None)
    parser.expect_console_echo(b"ENAble:VOLTage:DC 1,1\r\n")

    assert parser.parse_messages(first) == ([], 0), (
        f"a partial console line was consumed: {first!r}"
    )
    # Once whole, it is consumed and whatever follows still parses.
    messages, consumed = parser.parse_messages(first + rest + _reply())
    assert consumed > 0
    assert len(messages) == 1, "the reply behind the console line was lost"


def test_matching_an_echo_races_safely_against_new_sends() -> None:
    """r1 #3: send() mutated the echo list while the consumer was between
    its startswith() and its remove(), so remove() raised ValueError and
    the consumer discarded a byte. Match and removal are now one lock hold.

    ⚠ AN INTERMITTENT DETECTOR, labelled so a green run is not mistaken
    for proof: it did NOT trigger on 5c724724 within its one-second window,
    because the interleaving needs the eviction to land between the
    consumer's startswith() and its remove(). The deterministic evidence is
    structural and checkable by reading: on 5c724724 the ``remove()`` sat
    outside any lock, and there was no lock to be outside of; it is now
    inside the one hold that also does the matching.
    """
    import threading as _t

    parser = _parser()
    stop = _t.Event()
    errors = []

    def register():
        i = 0
        while not stop.is_set():
            parser.expect_console_echo(f"SYSTem:CMD{i}?\r\n".encode())
            i += 1

    def match():
        try:
            while not stop.is_set():
                parser.parse_messages(b"SYSTem:CMD0?\r\n" + _reply())
        except BaseException as exc:                              # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [_t.Thread(target=register, daemon=True),
               _t.Thread(target=match, daemon=True)]
    for t in threads:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in threads:
        t.join(5)
    assert errors == [], f"the echo list raced: {errors}"


def test_no_unconsumed_echo_is_silently_evicted() -> None:
    """r1 #4: the eight-entry cap evicted echoes still awaiting their
    replies, so nine sends before the consumer ran left the ninth reply
    stalled forever. A stall is not an acceptable cost for a bound."""
    parser = _parser()
    sent = [f"SYSTem:CMD{i}?".encode() for i in range(9)]
    for cmd in sent:
        parser.expect_console_echo(cmd + b"\r\n")

    # The FIRST command's echo must still be recognised -- it is the one an
    # eviction policy would have dropped.
    messages, consumed = parser.parse_messages(sent[0] + b"\r\n" + _reply())
    assert len(messages) == 1 and consumed > 0, (
        "the oldest unconsumed echo was evicted, so its reply stalled"
    )
    # ... and so must the ninth's.
    messages, consumed = parser.parse_messages(sent[8] + b"\r\n" + _reply())
    assert len(messages) == 1 and consumed > 0


def test_an_echo_with_trailing_whitespace_still_matches() -> None:
    """r1 #5: expect_console_echo() took a string and stripped it, so
    whitespace that get_bytes() really sends -- a trailing space -- was
    discarded and the echo never matched."""
    parser = _parser()
    on_the_wire = b"SYSTem:STReam:START 1000 \r\n"   # note the trailing space
    parser.expect_console_echo(on_the_wire)

    messages, consumed = parser.parse_messages(on_the_wire + _reply())
    assert len(messages) == 1, (
        "the echo did not match what was actually sent, so the reply "
        "behind it stalled"
    )
    assert consumed == len(on_the_wire) + len(_reply())


# ---- audit round 2 on a0471915 ------------------------------------------


def test_a_prompt_shaped_frame_split_at_eight_bytes_is_held_then_wins() -> None:
    """r2 #1: THE RESIDUAL IS GONE, and this is the test that says so.

    A 68-byte frame whose body begins ``AQIFI> `` arriving split so only
    its first 8 bytes are buffered used to be consumed as the prompt and
    destroyed. Holding until the frame reading could be REFUTED fixed that
    but introduced a stall (a prompt followed by a short reply never
    reaches 69 bytes). The prompt is now taken on refutation OR on
    corroboration -- a valid frame parsing cleanly at offset 8 -- and held
    when neither holds, which costs nothing and loses nothing.
    """
    body = b"AQIFI> " + bytes(range(61))
    frame = bytes([68]) + body
    parser = ProtobufMessageParser(lambda b: bytes(b))

    # Only the ambiguous first 8 bytes so far: neither reading is
    # established, so NOTHING is consumed.
    assert parser.parse_messages(frame[:8]) == ([], 0), (
        "the ambiguous prefix was consumed as a prompt before either "
        "reading was established"
    )
    # The rest arrives: the frame decodes, so the frame wins.
    messages, consumed = parser.parse_messages(frame)
    assert len(messages) == 1 and messages[0].data == body
    assert consumed == len(frame)


def test_a_prompt_followed_by_a_short_reply_is_not_stalled() -> None:
    """CONTROL / DESIGN GUARD -- passes on a0471915 too, by design.

    It does not discriminate: the previous revision took the prompt
    eagerly, so it never stalled. It is here because it is the counter-case
    that ruled out the pure-hold fix I nearly shipped, and it guards
    against anyone reintroducing one.

    A reply shorter than 61 bytes after a prompt never accumulates the 69
    bytes a refutation needs, so holding for refutation alone waited
    forever. Corroboration -- a valid frame at offset 8 -- resolves it at
    once.
    """
    parser = _parser()
    data = b"DAQIFI> " + _reply()
    assert len(data) < 69, "the point of this test is a buffer under 69 bytes"
    messages, consumed = parser.parse_messages(data)
    assert len(messages) == 1, (
        "a short reply behind a prompt was stalled waiting for bytes that "
        "were never coming"
    )
    assert consumed == len(data)


def test_every_refutation_path_consults_the_pending_prefix_rule() -> None:
    """r2 #2: the decode-exception path never called the pending-prefix
    check, so a long registered echo split mid-line lost bytes and stalled
    its reply. An 84-byte wifi-password echo is the reported shape."""
    # ⚠ The cut has to land where the DECODE path sees it, which took a
    # deliberate construction: the first byte must declare a length SHORTER
    # than the echo, so the frame reading has enough bytes to try, and
    # fails. "E" is 0x45 = 69, so an echo longer than 70 bytes reaches the
    # decode path while still incomplete. Cutting anywhere else is held by
    # the frame parser's own partial-frame logic, which is why my first
    # attempt at this test passed on the defective head.
    long_command = b"ENAble:VOLTage:DC " + b"9" * 60 + b",1\r\n"
    assert len(long_command) > 70 and long_command[0] == 0x45
    parser = _parser()
    parser.expect_console_echo(long_command)

    for cut in (71, 75, len(long_command) - 3):
        fresh = _parser()
        fresh.expect_console_echo(long_command)
        assert fresh.parse_messages(long_command[:cut]) == ([], 0), (
            f"a registered echo split at {cut} lost bytes on the decode path"
        )
    # Whole, it is consumed and the reply behind it survives.
    messages, consumed = parser.parse_messages(long_command + _reply())
    assert len(messages) == 1 and consumed == len(long_command) + len(_reply())


# ---- audit round 2 finding 3: the echo registry with echo OFF ------------
#
# Registration was unconditional, so a client that had turned echo off --
# which initialize() does, first thing -- registered an echo per send that
# nothing could ever match. 10k sends left 10k entries and every frame
# boundary scanned all of them.
#
# ⚠ RESIDUAL, stated because it is reachable and this fix does not cover
# it: echo lives in the per-client microrl console object and is NOT
# persisted (SCPI_SetEcho writes only that object; no NVM write exists),
# and it defaults ON at console init (microrl.c:434). So a process that
# connects WITHOUT calling initialize(), after some earlier process
# disabled echo on the same console within the same power cycle, sees echo
# genuinely off while this parser's state is None/unknown, and registers
# as before.
#
# ⚠ AND SKIPPING initialize() IS NOT THE ONLY WAY IN, which an earlier
# version of this comment implied. A caller that runs initialize() and
# THEN sends enable_device_echo() is back in the registering state, so a
# capture's registrations accumulate there too. Both routes are listed in
# the PR's known-limitations table. Suppressing registration there would need an inference from
# "frames parsed, no echo ever matched", and that inference is UNSOUND: a
# recurrence of the whitespace-mismatch bug this file already fixed once
# presents identically, and acting on it would convert a logged backlog
# into a silently stalled reply. The backlog warning (now repeating on
# each doubling) is the chosen signal for that path instead.


def test_registration_follows_the_echo_state_the_library_set() -> None:
    """Unknown registers, OFF does not, ON registers again.

    Only the OFF leg fails on a0471915 -- registration was unconditional
    there, so "an echo got registered" is true on both sides and the other
    two legs are shape guards, not discriminators.
    """
    parser = _parser()

    # (1) UNKNOWN: we have not set echo, so we must assume it is on. This
    # is the power-on default and it is the one direction that stalls.
    parser.expect_console_echo(b"SYSTem:SYSInfoPB?\r\n")
    assert len(parser._expected_echoes) == 1

    # (2) The disable itself IS echoed -- the console echoes characters as
    # they arrive, before the command is dispatched -- so it registers,
    # and only commands after it do not.
    parser.expect_console_echo(b"SYSTem:ECHO -1\r\n")
    assert len(parser._expected_echoes) == 2
    for i in range(500):
        parser.expect_console_echo(b"SYSTem:SYSInfoPB?\r\n")
    assert len(parser._expected_echoes) == 2, (
        f"500 sends with echo off left {len(parser._expected_echoes)} "
        "unmatchable entries; the registry grows without bound"
    )

    # (3) Re-enabling resumes registration. The enable command itself is
    # NOT echoed (echo was off when it arrived), so the count is unchanged
    # until the next command.
    parser.expect_console_echo(b"SYSTem:ECHO 1\r\n")
    assert len(parser._expected_echoes) == 2
    parser.expect_console_echo(b"SYSTem:SYSInfoPB?\r\n")
    assert len(parser._expected_echoes) == 3


def test_the_disable_commands_own_echo_is_still_consumed() -> None:
    """DESIGN GUARD -- passes on a0471915, which registered everything.

    It rules out the "clear the list when echo is turned off" shape: the
    device echoes `SYSTem:ECHO -1` before acting on it, so clearing on the
    transition drops an echo that is already in flight, and the parser
    then reads its first byte as a frame length. That is the very stall
    being fixed, reintroduced by the fix.
    """
    parser = _parser()
    sent = b"SYSTem:ECHO -1\r\n"
    parser.expect_console_echo(sent)

    messages, consumed = parser.parse_messages(sent + _reply())
    assert len(messages) == 1 and consumed == len(sent) + len(_reply()), (
        "the disable command's own echo was not recognised, so the reply "
        "behind it was lost"
    )


def test_echo_zero_does_not_disable_echo() -> None:
    """DESIGN GUARD -- passes on a0471915. Pins a firmware fact.

    `SYSTem:ECHO 0` does NOT turn echo off: the firmware's test is
    `echoOn > -1` (microrl.c:290 and :624, a DAQiFi modification), so only
    -1 does. Reading the parameter as a boolean would suppress
    registration while the device kept echoing -- the stalling direction.
    """
    parser = _parser()
    parser.expect_console_echo(b"SYSTem:ECHO 0\r\n")
    parser.expect_console_echo(b"SYSTem:SYSInfoPB?\r\n")
    assert len(parser._expected_echoes) == 2, (
        "ECHO 0 was read as 'off', but the device goes on echoing"
    )

    # Out of range: SCPI_SetEcho returns SCPI_RES_ERR and echo is
    # unchanged, so neither may be read as a disable.
    for rejected in (b"SYSTem:ECHO 2\r\n", b"SYSTem:ECHO -2\r\n"):
        fresh = _parser()
        fresh.expect_console_echo(rejected)
        fresh.expect_console_echo(b"SYSTem:SYSInfoPB?\r\n")
        assert len(fresh._expected_echoes) == 2, (
            f"{rejected!r} is rejected by the device but was read as a "
            "state change"
        )


def test_the_short_spelling_of_the_disable_is_recognised() -> None:
    """`SYST:ECHO -1` is the same command. The firmware accepts the full
    node or the node truncated at its first lowercase letter, so both
    spellings reach SCPI_SetEcho and both must be read here.
    """
    for spelling in (b"SYST:ECHO -1\r\n", b"systEM:echo -1\r\n"):
        parser = _parser()
        parser.expect_console_echo(spelling)
        parser.expect_console_echo(b"SYSTem:SYSInfoPB?\r\n")
        assert len(parser._expected_echoes) == 1, (
            f"{spelling!r} was not recognised as the disable, so "
            "registration continued with echo off"
        )


# ---- audit round 2 finding 4: which parser gets told ---------------------


def test_the_echo_and_the_bytes_go_to_the_same_connection() -> None:
    """A reconnect must not let send() pair a new stream with an old parser.

    ⚠ REWRITTEN in round 3. Round 2's version hooked ``_state_lock`` to
    swap the consumer between two reads inside ``send()``. That window no
    longer exists -- the stream and the consumer are now ONE published
    value, read once -- so the old test was simulating an impossible
    interleaving. The real window is the one it missed: ``transport.connect()``
    makes the new session's stream reachable through ``transport.stream``
    BEFORE ``connect()`` installs the consumer for it, and ``send()`` used
    to read the stream from the transport. This reproduces that state
    directly.

    The invariant asserted is the one that matters, rather than any
    particular outcome: the stream that received the bytes and the parser
    that was told about them belong to the SAME connection.
    """
    device = _fakes._make_device_with_serial_facade()
    try:
        # ⛔ Read through API that exists on BOTH revisions. An earlier
        # draft read device._link, which simply does not exist on the
        # defective head, so it failed with AttributeError -- "this API is
        # new" is not evidence of a defect.
        consumer = device._consumer
        published_stream = device._transport.stream
        assert published_stream is not None and consumer is not None

        # Exactly the mid-connect state: a new session's stream is
        # reachable through the transport while the published pair is
        # still the old one.
        class _NewSessionStream:
            def __init__(self):
                self.written = bytearray()

            def write(self, b):
                self.written.extend(bytes(b))
                return len(bytes(b))

            def flush(self):
                pass

        new_stream = _NewSessionStream()
        device._transport.stream = new_stream

        line = _scpi().get_device_info().get_bytes().rstrip(b"\r\n")
        try:
            device.send(_scpi().get_device_info())
        except Exception:
            # Refusing the send outright is a correct outcome too -- what
            # must not happen is a split pair.
            pass

        told = line in consumer._parser._expected_echoes
        if new_stream.written:
            assert told is False, (
                "the bytes went to the NEW session's stream while the echo "
                "was registered on the OLD connection's parser; that parser "
                "is not reading those bytes, and the one that is has no "
                "record of the command"
            )
        else:
            assert told, (
                "the bytes went to the published stream, so its own parser "
                "must have been told about them"
            )
    finally:
        try:
            device.disconnect()
        except Exception:
            pass


def test_the_state_machine_reads_the_librarys_own_echo_commands() -> None:
    """The bytes the library actually sends, not hand-written ones.

    ⚠ This is the gap a literal-bytes test cannot cover. `get_bytes()` has
    changed its whitespace before -- a trailing space it really does put
    on the wire is what broke echo matching once already -- and the state
    machine keyed on a hand-written spelling would go silently inert.
    """
    scpi = _scpi()
    parser = _parser()

    parser.expect_console_echo(scpi.disable_device_echo().get_bytes())
    before = len(parser._expected_echoes)
    parser.expect_console_echo(scpi.get_device_info().get_bytes())
    assert len(parser._expected_echoes) == before, (
        "disable_device_echo()'s real bytes were not read as the disable"
    )

    parser.expect_console_echo(scpi.enable_device_echo().get_bytes())
    parser.expect_console_echo(scpi.get_device_info().get_bytes())
    assert len(parser._expected_echoes) == before + 1, (
        "enable_device_echo()'s real bytes did not resume registration"
    )


# ---- audit round 3 -------------------------------------------------------
#
# Round 2 held the prompt while the frame reading was undecided. That is
# reverted here. These tests are why.


def test_a_trailing_prompt_before_an_echo_and_reply_does_not_stall() -> None:
    """ORDINARY traffic with echo on, and round 2 stalled it forever.

    ⛔ This is the shape round 2's own test missed. It checked "prompt then
    a short reply" with NO echo in between, and without the echo the reply
    sat at offset 8 and decoded, which satisfied the corroboration rule.
    Put the echo back -- and echo being ON is the premise of this entire
    ticket -- and offset 8 is text, so nothing decodes there, while the
    outer 68-byte reading of "DAQIFI> ..." can never be refuted with fewer
    than 69 bytes. Held, with the reply stuck behind it.
    """
    command = b"SYSTem:SYSInfoPB?\r\n"
    parser = _parser()
    parser.expect_console_echo(command)

    data = b"DAQIFI> " + command + _reply()
    assert len(data) < 69, (
        "the point of this test is that 69 bytes never accumulate, so the "
        "outer frame reading cannot be refuted"
    )

    messages, consumed = parser.parse_messages(data)
    assert len(messages) == 1, (
        "a trailing prompt in front of the next command's echo and reply "
        "stalled the reply: this is the failure the ticket exists to remove"
    )
    assert consumed == len(data)


def test_a_prompt_then_a_short_reply_is_still_not_stalled() -> None:
    """The round-2 case, kept: it must hold under the reverted rule too."""
    parser = _parser()
    data = b"DAQIFI> " + _reply()
    assert len(data) < 69
    messages, consumed = parser.parse_messages(data)
    assert len(messages) == 1 and consumed == len(data)


@pytest.mark.xfail(
    reason="ACCEPTED RESIDUAL on #231, not a fix in flight: taking the "
           "prompt while the frame reading is merely incomplete can emit a "
           "message that was never sent. Asserting the CORRECT behaviour "
           "and marking it expected-to-fail, so this test never REQUIRES "
           "the defect and turns into an XPASS the moment it is fixed.",
    strict=False,
)
def test_the_accepted_residual_can_fabricate_a_message() -> None:
    """⛔ A real frame can begin with the literal bytes ``DAQIFI> ``.

    Constructed, not hypothesised. 0x41 (``A``) is field 8 wire type 1, a
    fixed64, so it consumes body[1:9] and the outer body's parse resumes
    at body[9] -- inside what a sub-frame placed at body offset 7 used as
    its own content. Both readings are then individually valid, so a split
    delivery takes the eight bytes as a prompt and decodes the sub-frame,
    emitting a message the device never sent.

    This also shows why round 2's "corroborate by decoding at offset 8"
    was unsound: a fragment of a real body decodes, so a decode at an
    offset is not evidence about the bytes before it.
    """
    body = bytearray(b"AQIFI> ")       # body[0:7]
    body += b"\x02"                    # body[7]    sub-frame length 2
    body += b"\x08\x08"                # body[8:10] sub-frame body
    body += b"\x81\x01"                # body[10:12] value for body[9]'s tag
    body += b"\x08\x01" * 28           # pad to exactly 68, field-aligned
    body = bytes(body)
    assert len(body) == 68
    DaqifiOutMessage.FromString(body)          # the OUTER frame is real
    DaqifiOutMessage.FromString(body[8:10])    # so is the sub-frame

    full = bytes([68]) + body
    assert full[:8] == b"DAQIFI> ", full[:8]

    messages, _consumed = _parser().parse_messages(full[:11])
    assert messages == [], (
        "a message was fabricated from inside a real frame's body"
    )


def test_the_capture_residual_is_bounded_by_the_echo_state() -> None:
    """⚠ PINS AN ACCEPTED RESIDUAL AND THE THING THAT BOUNDS IT.

    A capture pauses the protobuf consumer, so an echo registered on its
    parser during one can never be consumed. Round 3 added a device-wide
    flag to suppress that registration; the flag caused two HIGH defects
    (it also froze the echo-state transition, and it leaked stuck-True on
    a raise) and was REVERTED, because the echo-state tracking already
    closes the case on every path that calls ``initialize()``.

    So the residual is: **no initialize(), with echo on or unknown**. That
    single precondition has two symptoms -- the registry grows, and
    captures leave unmatchable entries. Measured here rather than
    asserted: 5 of 5 leak with the state unknown, 0 of 5 once the library
    has sent the disable.

    This is not a control and not a defect repro; it is the boundary of an
    accepted residual. It fails if someone makes the leak unconditional --
    which is the only change that would turn a documented limitation back
    into a bug.
    """
    scpi = _scpi()

    def _leak(disable_echo_first: bool) -> int:
        device = _fakes._make_device_with_serial_facade(
            {b"SYSTem:SYSInfoPB?": b'0,"No error"\r\n',
             b"SYSTem:ECHO": b'0,"No error"\r\n'}
        )
        try:
            parser = device._consumer._parser
            if disable_echo_first:
                device.send(scpi.disable_device_echo())   # initialize() does this
            before = len(parser._expected_echoes)
            for _ in range(5):
                device.execute_text_command(
                    lambda: device.send(scpi.get_device_info()),
                    response_timeout_ms=60,
                    completion_timeout_ms=30,
                )
            return len(parser._expected_echoes) - before
        finally:
            try:
                device.disconnect()
            except Exception:
                pass

    assert _leak(disable_echo_first=True) == 0, (
        "a capture leaked an unmatchable echo even though the library had "
        "turned echo OFF; the echo-state check is what bounds this residual, "
        "so the residual is no longer bounded"
    )
    assert _leak(disable_echo_first=False) == 5, (
        "the documented exposure changed shape: captures with the echo state "
        "unknown should leave exactly one unmatchable entry per send"
    )


def _tag_bytes_the_encoder_can_emit() -> dict:
    """First body byte protobuf actually emits for each field, by asking
    the encoder rather than by tabulating wire types by hand.

    One field is set at a time and the message serialized; the first byte
    of the result is that field's tag as the real encoder writes it, which
    covers packed repeated fields without any special-casing here.
    """
    from google.protobuf.descriptor import FieldDescriptor as FD

    def _repeated(field) -> bool:
        # `.label` is deprecated in current protobuf and `is_repeated()`
        # does not exist in the oldest one this package supports
        # (python_requires >= 3.9), so prefer the new accessor and fall
        # back rather than emitting a DeprecationWarning per field.
        # ⚠ It is a PROPERTY in protobuf 6.x and a METHOD in others, so a
        # `callable()` test alone silently fell through to the deprecated
        # `.label` and kept emitting the warning it was added to avoid.
        probe = getattr(field, "is_repeated", None)
        if isinstance(probe, bool):
            return probe
        if callable(probe):
            return bool(probe())
        return field.label == FD.LABEL_REPEATED

    emitted = {}
    for field in DaqifiOutMessage.DESCRIPTOR.fields:
        msg = DaqifiOutMessage()
        try:
            if _repeated(field):
                if field.type == FD.TYPE_MESSAGE:
                    getattr(msg, field.name).add()
                elif field.type in (FD.TYPE_STRING,):
                    getattr(msg, field.name).append("x")
                elif field.type in (FD.TYPE_BYTES,):
                    getattr(msg, field.name).append(b"x")
                else:
                    getattr(msg, field.name).append(1)
            elif field.type == FD.TYPE_MESSAGE:
                getattr(msg, field.name).SetInParent()
            elif field.type == FD.TYPE_STRING:
                setattr(msg, field.name, "x")
            elif field.type == FD.TYPE_BYTES:
                setattr(msg, field.name, b"x")
            elif field.type == FD.TYPE_BOOL:
                setattr(msg, field.name, True)
            else:
                setattr(msg, field.name, 1)
        except Exception:                                    # noqa: BLE001
            continue
        raw = msg.SerializeToString()
        if raw:
            emitted[raw[0]] = field.name
    return emitted


def test_no_real_frame_body_can_begin_with_the_prompts_second_byte() -> None:
    """⛔ THE GUARD ON THE ACCEPTED RESIDUAL at ``_take_console_noise``.

    The whole prompt-versus-frame overlap needs ``DAQIFI> `` to be readable
    as a frame: length ``0x44`` = 68, then a body beginning ``AQIFI> ``. A
    protobuf body begins with a TAG byte, and ``0x41`` ('A') means field 8
    with wire type 1 (fixed64). Field 8 is ``device_status``, a uint32, so
    the encoder emits ``0x40`` — and no other field in this schema yields
    ``0x41``. So no frame the firmware can produce has a body starting
    ``AQIFI> ``, and the residual is unreachable for schema-conformant
    output rather than merely improbable.

    ⚠ That is a property of THIS SCHEMA, not a structural one. Add a
    64-bit field numbered 8 — a double, fixed64 or sfixed64 — and ``0x41``
    becomes emittable and the residual goes LIVE, including its worst form:
    emitting a message that was never sent. This test is here to fail on
    the day that happens, which is the only warning anyone will get.
    """
    emitted = _tag_bytes_the_encoder_can_emit()

    # Positive control: a probe that silently set nothing would make the
    # assertion below vacuous.
    assert len(emitted) >= 10, (
        f"only {len(emitted)} tag bytes were derived, so this test is not "
        "actually exercising the schema"
    )
    assert 0x40 in emitted and emitted[0x40] == "device_status", (
        "field 8 is no longer device_status-as-uint32; re-derive the "
        "residual argument before trusting this guard"
    )

    prompt_second_byte = ProtobufMessageParser._CONSOLE_PROMPT[1]
    assert prompt_second_byte == 0x41
    assert prompt_second_byte not in emitted, (
        f"0x41 is now emittable as a first body byte (field "
        f"{emitted.get(prompt_second_byte)!r}), so a REAL frame can begin "
        f"with the bytes {bytes(ProtobufMessageParser._CONSOLE_PROMPT)!r}. "
        "The #231 residual documented on _take_console_noise is no longer "
        "unreachable: taking the prompt while the frame reading is merely "
        "incomplete can now destroy a real frame and emit a fabricated "
        "message. Reopen it."
    )


# ---- echo known OFF: the supported automation mode ----------------------
#
# Measured on an NQ1, with echo 1 run first as a control: at echo -1 a reply
# carries no 'DAQIFI> ' and no echoed command, while at echo 1 both appear.
# So in the known-off state there is nothing for the echo match or the
# prompt skip to do, and not doing them keeps their failure modes out of the
# path automation actually uses. initialize() sends the disable first thing.
#
# ⛔ '**ERROR: ' lines are NOT covered: the same measurement showed the
# device still emits them with echo off.


def _raw_parser():
    """A parser whose factory accepts ANY bytes -- the shape that makes the
    0x41-is-not-an-emittable-tag argument inapplicable, because there is no
    schema to constrain the body."""
    return ProtobufMessageParser(lambda b: b)


def test_with_echo_known_off_a_prompt_shaped_frame_is_returned_intact() -> None:
    """The r5 HIGH, closed for the automation path.

    With a raw-bytes factory there is no schema, so a 68-byte frame whose
    body starts ``AQIFI> `` is perfectly legal and the prompt skip would
    destroy it -- or worse, decode a fragment of it. Once echo is known
    off the prompt cannot arrive at all, so the frame wins outright.
    """
    parser = _raw_parser()
    parser.expect_console_echo(b"SYSTem:ECHO -1\r\n")
    # Drain the disable's own echo: it IS emitted, because echo was still
    # on when it arrived. Only afterwards is the console provably quiet.
    parser.parse_messages(b"SYSTem:ECHO -1\r\n")

    body = b"AQIFI> " + bytes(61)
    frame = bytes([68]) + body
    assert frame[:8] == b"DAQIFI> ", frame[:8]

    # ⚠ The split has to leave MORE than the prompt buffered. At exactly
    # 8 bytes the old code held anyway -- `_CONSOLE_PROMPT.startswith()` is
    # not strict, so a complete prompt counts as its own pending prefix --
    # and a test cut there passes on the defective head. Past 8 bytes that
    # rule no longer applies and the prompt skip fires, eating the frame's
    # first 8 bytes.
    held, consumed = parser.parse_messages(frame[:40])
    assert (held, consumed) == ([], 0), (
        f"{consumed} bytes of a real frame were taken as a console prompt"
    )
    messages, consumed = parser.parse_messages(frame)
    assert len(messages) == 1 and consumed == len(frame), (
        "a 68-byte frame beginning 'AQIFI> ' did not survive with echo off"
    )
    assert messages[0].data == body


def test_an_error_line_is_still_handled_with_echo_off() -> None:
    """GUARD -- passes on 65e564a9 too, and cannot discriminate.

    It protects behaviour this change deliberately did NOT alter, so there
    is no defective head for it to fail on. It is here because "no console
    skipping when echo is off" was the instruction, and this is the third
    of it that the board refuted; without this test the next reader has
    nothing stopping them from completing the change as originally worded.

    ⛔ The part of "no console skipping" that must NOT happen.

    The device still emits ``**ERROR: `` with echo off -- measured:
    ``NOSUCH?`` answered ``**ERROR: -113, "Undefined header"`` with no
    prompt around it. Handed to the frame parser, ``0x2a`` reads as a
    42-byte length prefix, so the line would either decode into a
    fabricated message or swallow the next real reply into its body.
    """
    seen = []
    parser = _parser(console_error_sink=seen.append)
    parser.expect_console_echo(b"SYSTem:ECHO -1\r\n")
    parser.parse_messages(b"SYSTem:ECHO -1\r\n")       # drain its own echo

    data = b'**ERROR: -113, "Undefined header"\r\n' + _reply()
    messages, consumed = parser.parse_messages(data)
    assert len(messages) == 1 and consumed == len(data), (
        "an error line with echo off was read as a frame, so the reply "
        "behind it was lost"
    )
    assert seen == ['**ERROR: -113, "Undefined header"'], (
        f"the error line was dropped rather than reported: {seen}"
    )


def test_a_signed_plus_one_turns_echo_back_on() -> None:
    """``SYSTem:ECHO +1`` is the same command as ``SYSTem:ECHO 1``.

    ⚠ This became load-bearing with the rule above. Before it, an
    unrecognised enable cost unmatched registrations; now it means the
    library believes echo is off while the device echoes, so NOTHING is
    skipped and the echoed line stalls the reply -- the defect this ticket
    exists to remove.
    """
    for spelling in (b"SYSTem:ECHO +1\r\n", b"SYST:ECHO +1\r\n"):
        parser = _parser()
        parser.expect_console_echo(b"SYSTem:ECHO -1\r\n")
        parser.parse_messages(b"SYSTem:ECHO -1\r\n")   # drain, now quiet
        parser.expect_console_echo(spelling)           # not echoed: echo off
        command = b"SYSTem:SYSInfoPB?\r\n"
        parser.expect_console_echo(command)

        messages, consumed = parser.parse_messages(command + _reply())
        assert len(messages) == 1 and consumed == len(command) + len(_reply()), (
            f"{spelling!r} was not read as enabling echo, so the echoed "
            "command was handed to the frame parser and stalled the reply"
        )


# ---- every spelling the firmware accepts -------------------------------
#
# ⚠ An EN A revision also had initialize() QUERY SYSTem:ECHO? so the echo
# state was never unknown. Ruling ET B removed it: a plain-text reply on a
# stream a frame parser reads can arrive after any bounded window, and a
# late one was read as a 45-byte length that swallowed the next real reply.
# The regex below is the part that survived -- it adds no wire traffic.


def test_every_spelling_the_firmware_accepts_is_recognised() -> None:
    """⛔ A MISSED SPELLING IS A STALLED REPLY.

    Under the known-off rule, an enable the library fails to recognise
    leaves it believing echo is off, so it skips nothing while the device
    echoes -- the stall this whole ticket exists to remove.

    Each spelling below was verified ON AN NQ1 to take effect with a clean
    error queue, not assumed from the SCPI grammar: a leading colon
    (absolute path), ``SYST`` or ``SYSTem``, any case, and a signed value.
    """
    cases = [
        (b":SYST:ECHO 1\r\n", True),
        (b":SYSTem:ECHO -1\r\n", False),
        (b"SYSTem:ECHO +1\r\n", True),
        (b"syst:echo -1\r\n", False),
        (b"SYST:ECHO 1\r\n", True),
        # ⚠ 0 is NOT off: the firmware's test is `echoOn > -1`
        # (microrl.c:290), confirmed on the wire -- after ECHO 0 the next
        # reply still comes back echoed.
        (b"SYSTem:ECHO 0\r\n", True),
    ]
    for spelling, expected_on in cases:
        parser = _parser()
        parser.expect_console_echo(spelling)
        got = parser._echo_enabled
        assert got is expected_on, (
            f"{spelling!r} was read as echo={got}, expected {expected_on}; "
            "a spelling the device accepts but we do not leaves the "
            "library out of step with the console"
        )


