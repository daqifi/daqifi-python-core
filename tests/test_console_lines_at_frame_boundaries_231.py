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

import pytest

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.communication import ProtobufMessageParser


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
