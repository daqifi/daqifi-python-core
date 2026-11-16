"""One definition of what a device does with the download's echo SETTER.

⛔ THIS REPLACED `_sd_echo_probe.py` (#260 r6). That file modelled a reply to
`SYSTem:ECHO?`, and the r5 audit established that no such reply can be
attributed to the probe that asked for it -- the query takes no nonce, so
every probe's reply is the same four bytes, and the per-object flag that
stood in for attribution does not survive a new connection object. The
download now SETS the state instead, so there is no reply to model and
nothing to attribute.

⛔ EVERY SD TEST MODULE THAT INSTALLS A FAKE PORT MUST USE THIS, and the
reason is mechanical rather than stylistic: the download writes two setter
lines BEFORE the GET. A fake that treats any write as "the GET has arrived"
releases its payload on the first setter line, and a fake that never echoes
the setter makes every download refuse. Eight hand-copied versions of that
behaviour is the duplicated-definition pattern this series keeps paying for.
`test_every_sd_fake_answers_the_setter_through_the_shared_helper` pins it.
"""

import time

#: What the DOWNLOAD writes, in order. The short spelling is deliberate and
#: load-bearing: open() writes the FULL `SYSTem:ECHO -1` and close() writes
#: `SYSTem:ECHO 1`, so a full-spelling marker here would be byte-identical
#: to open()'s echo and the download would synchronise on the wrong one.
ECHO_SET_FRAMED = b"SYST:ECHO 1"

#: ⛔ THE WHOLE FAMILY, because the download picks one at RANDOM. `-1`,
#: `-01`, `-001`, ... all set the console silent and the device echoes
#: whichever it was sent, verbatim.
#:
#: ⛔⛔ A FAKE NARROWER THAN ITS SUBJECT FAILS SILENTLY AND MISLEADINGLY.
#: When the implementation grew from 8 spellings to 128 and this helper
#: still knew only `SYST:ECHO -1`, every other spelling stopped being
#: recognised as echo traffic -- so the fakes treated the SETTER as the GET
#: and released their payload early, and eight unrelated cells (case-D,
#: setter-before-GET, state-per-download, no-flush) went red for a reason
#: that had nothing to do with what they test.
#: `test_the_shared_helper_covers_every_implementation_spelling` pins it.
ECHO_SET_SILENT_FAMILY = tuple(
    b"SYST:ECHO -" + b"0" * n + b"1" for n in range(128))
ECHO_SET_SILENT = ECHO_SET_SILENT_FAMILY[0]

#: What open() and close() write. Present so a fake can model them as the
#: distinct commands they are.
OPEN_SET_SILENT = b"SYSTem:ECHO -1"
CLOSE_SET_FRAMED = b"SYSTem:ECHO 1"

PROMPT = b"DAQIFI> "


def echo_setter_reply(written, echo_state):
    """-> (reply_or_None, new_echo_state) for one write.

    ``reply_or_None`` is None when ``written`` was not echo-state traffic.

    ⚠ THE ECHO IS GATED ON THE STATE AS THE CHARACTERS ARRIVE, not on the
    state the line sets. microrl echoes while reading the line, so
    `SYST:ECHO 1` sent to a silent console is NOT echoed even though echo is
    on once it has been processed -- and the line AFTER it is. That ordering
    is the whole reason the download writes the pair: it makes the second
    line's echo certain from any starting state.
    """
    text = bytes(written)
    # Longest first: a scan that matched `-1` inside `-01` would answer with
    # the wrong spelling, which is the very confusion the family exists to
    # prevent.
    for cmd in sorted(ECHO_SET_SILENT_FAMILY + (OPEN_SET_SILENT,),
                      key=len, reverse=True):
        if cmd in text:
            reply = (cmd + b"\r\n" + PROMPT) if echo_state != -1 else b""
            return reply, -1
    for cmd in (ECHO_SET_FRAMED, CLOSE_SET_FRAMED):
        if cmd in text:
            reply = (cmd + b"\r\n" + PROMPT) if echo_state != -1 else b""
            return reply, 1
    return None, echo_state


def transport_cannot_silence(device):
    """Mark a device's transport as one whose console state cannot be SET,
    and return it.

    ⛔ FOR PINS WHOSE SUBJECT IS THE STRIP, not the setter. #154, #206 and
    #257 pin that the receiver removes an echoed GET and its prompt from the
    head of a download. On a conforming device that case no longer arises
    through `sd_get_file`, because the download silences the console first
    and the GET is therefore never echoed -- which is exactly what lets a
    case-D payload survive intact (#260's actual goal).

    The strip path is still real and still reachable: a transport that
    cannot set the state at all (any `_comm` that is not a SerialInterface),
    or a device that ignores the setter. `_silence_echo_before_get` returns
    None for the first, and the receiver then falls back to stripping the
    GET command exactly as it did before #260. Routing those pins through
    this keeps their subject and their original assertions, rather than
    restating them around a guarantee they were never about.

    ⚠ IT IS NOT AN EXEMPTION FROM ANSWERING THE SETTER. A fake that is
    simply silent to the setter makes every download refuse, and r5's
    finding 2 is what happens when such a fake is waved through: all six of
    #263's cases failed inside the probe, never reached either GET write,
    and an unbounded flush() on both GET drains still passed. This removes
    the setter from the conversation entirely instead of leaving a fake to
    ignore it.
    """
    device._comm.silence_echo_for_download = lambda: None
    return device


class EchoSetterFake:
    """Mixin: answer the download's echo setter, and keep the payload back
    until the GET actually arrives.
    """

    #: The console state this fake starts in. 1 (a framed console) by
    #: default, so the setter pair has something to silence and every fake
    #: exercises the echo it produces.
    echo_state = 1

    #: #260 r4: a real port exposes this, and drain_output_bounded() REFUSES
    #: a port that does not ("refusing an unbounded flush"). Zero means the
    #: output queue is already empty, which is true of every fake here --
    #: they accept writes synchronously.
    out_waiting = 0

    #: The real close() calls these, so a fake driven through the actual
    #: close()/open() cycle must have them.
    def close(self):
        self.is_open = False

    def cancel_read(self):
        pass

    def cancel_write(self):
        pass

    def reset_output_buffer(self):
        pass

    _console_reply = b""
    _console_armed = True

    #: ``(delay_s, bytes)`` entries delivered that many seconds after the
    #: setter was written, instead of the reply appearing at once. Needed to
    #: model bytes that are still IN FLIGHT, which a reset cannot discard.
    echo_reply_schedule = None
    _console_sent_at = None

    def _console_write(self, data):
        """True if ``data`` was echo-state traffic; queues its echo and
        withholds the payload. False for any other command, which re-arms
        the payload."""
        reply, state = echo_setter_reply(data, self.echo_state)
        if reply is None:
            self._console_armed = True
            # ⛔ DO NOT CLEAR `_console_sent_at` HERE. The GET is a non-setter
            # write, and clearing it stopped the schedule dead at exactly the
            # moment the hazard lives in. Bytes in flight do not stop being
            # in flight because the host sent something else.
            return False
        self.echo_state = state
        self._console_armed = False
        self._console_sent_at = time.monotonic()
        if self.echo_reply_schedule is None:
            self._console_reply += reply          # arrives at once
        return True

    def _console_due(self):
        if self.echo_reply_schedule is None or self._console_sent_at is None:
            return
        elapsed = time.monotonic() - self._console_sent_at
        pending = [c for d, c in self.echo_reply_schedule if elapsed >= d]
        delivered = getattr(self, "_console_delivered", 0)
        for i, chunk in enumerate(pending):
            if i >= delivered:
                self._console_reply += chunk
        self._console_delivered = max(delivered, len(pending))

    def _console_pending(self, otherwise):
        """``in_waiting``: the setter echo while one is outstanding, else 0
        while disarmed, else whatever the fake would have reported."""
        self._console_due()
        if self._console_reply:
            return len(self._console_reply)
        return otherwise() if self._console_armed else 0

    def _console_read(self, n, otherwise):
        """``read``: drain the setter echo first, then the fake's own bytes."""
        self._console_due()
        if self._console_reply:
            out = self._console_reply[:n]
            self._console_reply = self._console_reply[n:]
            return out
        return otherwise() if self._console_armed else b""

    def _console_reset(self):
        """``reset_input_buffer``: discard an unconsumed echo.

        ⛔ IT DELIBERATELY DOES NOT CLEAR THE SCHEDULE. Bytes still in flight
        stay in flight, because that is the truth a reset cannot change.
        """
        self._console_reply = b""
