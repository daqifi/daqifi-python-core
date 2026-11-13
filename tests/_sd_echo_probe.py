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

import time

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

    #: #260 r2: IN-FLIGHT MODELLING. A list of ``(delay_s, bytes)`` delivered
    #: that many seconds after the probe was written, instead of the whole
    #: reply appearing at once.
    #:
    #: ⛔ WITHOUT THIS THE FAKES CANNOT EXPRESS THE DEFECT r1 SHIPPED. r1's
    #: own "late reply" pin had the reply ALREADY QUEUED when the probe gave
    #: up, so `reset_input_buffer()` swept it and the pin passed -- hiding both
    #: real cases. A reply that is still ON THE WIRE cannot be reset away, and
    #: only a schedule can model that.
    echo_reply_schedule = None

    _probe_sent_at = None

    def _probe_write(self, data):
        """True if ``data`` was a probe; queues the reply and withholds the
        payload. False for any other command, which re-arms the payload."""
        reply = echo_probe_reply(data, self.echo_state)
        if reply is None:
            self._probe_armed = True
            # ⛔ DO NOT CLEAR `_probe_sent_at` HERE. The GET is a non-probe
            # write, and clearing it stopped the schedule dead at exactly the
            # moment the hazard lives in -- so a remainder "arriving at 120 ms"
            # was never delivered at all and 40 split pins passed against the
            # r1 defect they were written to catch. Bytes in flight do not
            # stop being in flight because the host sent something else.
            return False
        self._probe_armed = False
        self._probe_sent_at = time.monotonic()
        if self.echo_reply_schedule is None:
            self._probe_reply = reply           # arrives at once
        else:
            self._probe_reply = b""             # arrives per the schedule
        return True

    def _probe_due(self):
        """Bytes whose scheduled delay has elapsed, appended as they land."""
        if self.echo_reply_schedule is None or self._probe_sent_at is None:
            return
        elapsed = time.monotonic() - self._probe_sent_at
        pending = []
        for delay, chunk in self.echo_reply_schedule:
            if elapsed >= delay:
                pending.append((delay, chunk))
        # Deliver each scheduled chunk exactly once.
        delivered = getattr(self, "_probe_delivered", 0)
        for i, (_d, chunk) in enumerate(pending):
            if i >= delivered:
                self._probe_reply += chunk
        self._probe_delivered = max(delivered, len(pending))

    def _probe_pending(self, otherwise):
        """``in_waiting``: the reply while one is outstanding, else 0 while
        disarmed, else whatever the fake would have reported."""
        self._probe_due()
        if self._probe_reply:
            return len(self._probe_reply)
        return otherwise() if self._probe_armed else 0

    def _probe_read(self, n, otherwise):
        """``read``: drain the reply first, then the fake's own bytes."""
        self._probe_due()
        if self._probe_reply:
            out = self._probe_reply[:n]
            self._probe_reply = self._probe_reply[n:]
            return out
        return otherwise() if self._probe_armed else b""

    def _probe_reset(self):
        """``reset_input_buffer``: discard an unconsumed reply.

        ⚠ #260 r2: NOT A SAFETY NET, and r1 mistook it for one. This discards
        what has ALREADY ARRIVED -- exactly what pyserial does -- and can do
        nothing about bytes the device has yet to send. r1 leaned on it and
        still shipped two contamination cases. The real guarantee is now the
        CONTENT completion rule, which does not return until the whole reply
        is in hand; this is kept only so the fakes behave like a real port.

        ⛔ IT DELIBERATELY DOES NOT CLEAR THE SCHEDULE. Bytes still in flight
        stay in flight, because that is the truth a reset cannot change.
        """
        self._probe_reply = b""
