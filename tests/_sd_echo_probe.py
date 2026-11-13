"""#260: the ONE definition of how a test fake answers the echo probe.

⛔ A DEDICATED MODULE, NOT `conftest`. This lived in `tests/conftest.py`
first and broke the whole suite: several directories here have a `conftest.py`,
so `from conftest import ...` resolved to `tests/integration/hardware/`'s copy
and every SD module failed to import. Running one file at a time hid it,
because then the right directory happened to be first on `sys.path`. A unique
module name cannot collide, and helper classes do not belong in a conftest
anyway -- that file is for fixtures and hooks.
"""

# ---------------------------------------------------------------------------
# #260: what a device sends back to the echo probe. ONE definition.
# ---------------------------------------------------------------------------

ECHO_PROBE = b"SYSTem:ECHO?"


def echo_probe_reply(written, echo_state=1, command=ECHO_PROBE):
    """The bytes a device in ``echo_state`` answers an ECHO? probe with, or
    ``None`` when ``written`` is not a probe.

    ⛔ WIRE-FAITHFUL ON PURPOSE, because the lazy version hides the bug #260
    exists for. `microrl` gates the prompt on ``echoOn > -1``, so:

        -1   console silent        -> bare value line
         0   prompt AND echo       -> echoed command, value, prompt
         1   prompt AND echo       -> IDENTICAL to 0

    A fake that answered a bare ``0`` would make the library decline to
    confirm -- the right outcome, for the wrong reason -- and would NOT catch
    an implementation that treated 0 as silent. The whole point of #260 is
    that 0 is not "off", so the fakes have to frame 0's reply like 1's.

    ⭐ ONE DEFINITION, used by every SD fake. The per-fake code is only the
    ADAPTER that queues these bytes, because the fakes genuinely differ in how
    they hold pending data (bytearray, chunk list, position index); the
    POLICY -- what a device would send -- lives only here. There is a
    meta-test asserting no SD fake answers a probe without calling this.
    """
    if ECHO_PROBE not in bytes(written):
        return None
    state = int(echo_state)
    value = str(state).encode("ascii")
    if state <= -1:
        return value + b"\r\n"
    return bytes(command) + b"\r\n" + value + b"\r\nDAQIFI> "


class EchoProbeFake:
    """#260: the ONE implementation of "this fake answers the echo probe".

    ⛔ WHY A MIXIN AND NOT A COPIED HOOK. Eight hand-copied four-line hooks is
    the duplicated-definition pattern this series has already paid for twice
    (`head_span` vs `feed`/`flush`, tracked as #264). The policy -- what a
    device sends, and that the payload must be WITHHELD while a probe reply is
    outstanding -- lives here only. A fake supplies three one-line
    delegations, because the fakes genuinely differ in how they hold pending
    bytes (bytearray, chunk list, timed release) and that part is irreducible.

    Usage, in each fake::

        def write(self, data):
            if self._probe_write(data):
                return len(data)
            ...existing...

        @property
        def in_waiting(self):
            return self._probe_pending(lambda: <existing count>)

        def read(self, n):
            return self._probe_read(n, lambda: <existing read>)

    ⚠ THE WITHHOLDING IS THE POINT, not the reply. The library probes BEFORE
    writing the GET, so a fake that serves its payload to any read hands the
    FILE to the probe. Between a probe and the next real command this serves
    the reply and then nothing.

    ⚠ But a fake handed straight to ``SdCardFileReceiver`` as a raw SOURCE is
    never written to at all, so the payload must be readable by default. Hence
    ``_probe_armed`` starts True and only a probe disarms it.
    """

    #: The echo state this fake models. 1 (framed console) by default, so every
    #: test written for the byte-shape path keeps exercising it.
    echo_state = 1

    _probe_reply = b""
    _probe_armed = True

    def _probe_write(self, data):
        """True if ``data`` was a probe; queues the reply and withholds the
        payload. False for any other command, which re-arms the payload."""
        reply = echo_probe_reply(data, self.echo_state)
        if reply is None:
            self._probe_armed = True
            return False
        self._probe_reply = reply
        self._probe_armed = False
        return True

    def _probe_pending(self, otherwise):
        """``in_waiting``: the reply while one is outstanding, else 0 while
        disarmed, else whatever the fake would have reported."""
        if self._probe_reply:
            return len(self._probe_reply)
        return otherwise() if self._probe_armed else 0

    def _probe_read(self, n, otherwise):
        """``read``: drain the reply first, then the fake's own bytes."""
        if self._probe_reply:
            out = self._probe_reply[:n]
            self._probe_reply = self._probe_reply[n:]
            return out
        return otherwise() if self._probe_armed else b""

    def _probe_reset(self):
        """``reset_input_buffer``: discard an unconsumed reply.

        ⛔ LOAD-BEARING, not tidiness. pyserial discards pending input here and
        the probe relies on it: a reply that arrives too late for the probe to
        consume would otherwise bleed into the file that follows. Measured
        with this stubbed out as ``pass``: 25 bytes of probe reply landed
        inside a 256 KiB download.
        """
        self._probe_reply = b""
