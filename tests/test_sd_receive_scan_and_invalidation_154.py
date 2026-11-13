"""r10's three regressions: the receive loop's cost, its deadline order, and a
listing publish that resurrected invalidated sizes (#154, #206, #257 r10).

⛔ ALL THREE ARE REGRESSIONS AGAINST THE BASE (01343229), not pre-existing
gaps, so each test below is a differential: it must FAIL at 5309e5ff and pass
after. None of them sleeps or races -- the two timing ones drive a scripted
source with no delays, and the invalidation one uses thread events pinned at
the publish, so the interleaving is exact rather than hoped for.
"""
import threading
import time

import pytest

from daqifi.device import NyquistDevice
from daqifi.sdcard import END_OF_FILE_MARKER


class _ChunkedSerial:
    """A serial-shaped source that hands out `payload` in fixed-size reads.

    No delays: `in_waiting` is always the next chunk's size until the payload
    is exhausted. That makes the loop's own cost the only thing that can
    consume the timeout, which is what the two timing tests measure.
    """
    out_waiting = 0  # #263: the send path drains this queue, bounded

    def __init__(self, payload: bytes, chunk: int = 4096):
        self._payload = payload
        self._chunk = chunk
        self._pos = 0
        self.is_open = True
        self.timeout = 0.1

    @property
    def in_waiting(self) -> int:
        return min(self._chunk, len(self._payload) - self._pos)

    def read(self, n: int = 1) -> bytes:
        end = min(self._pos + min(n, self._chunk), len(self._payload))
        out = self._payload[self._pos:end]
        self._pos = end
        return out

    def write(self, data):            # the command write; nothing to answer
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False

    @property
    def unread(self) -> int:
        return len(self._payload) - self._pos


def _device_with(payload: bytes, chunk: int = 4096):
    device = NyquistDevice("COM41")
    ser = _ChunkedSerial(payload, chunk)
    device._comm._serial = ser
    return device, ser


# --------------------------------------------------------------------------
# :1028 -- the whole-buffer copy and rescan per iteration
# --------------------------------------------------------------------------

def test_a_large_download_is_not_quadratic_in_the_bytes_received():
    """⛔ FAILS AT 5309e5ff: TimeoutError at 1.0 s with ~10.6 MB unread.

    `buf = bytes(out)` copied everything received so far and
    `END_OF_FILE_MARKER in buf[head:]` rescanned it, on EVERY iteration --
    O(n^2) over a download.

    ⭐ THE SIZE AND TIMEOUT ARE MEASURED, NOT CHOSEN. An earlier version of
    this test used 8 MiB to avoid a machine-dependent threshold and PASSED ON
    THE DEFECT, because 8 MiB is below where the quadratic cost bites. The
    measurements, this box, both implementations:

        payload   fixed     r10 (broken)
         8 MiB    0.01 s    2.08 s   completes
        16 MiB    0.02 s    8.55 s   completes
        32 MiB    0.06 s    TimeoutError at 30 s

    16 MiB with a 1.0 s timeout therefore separates them by a wide margin in
    BOTH directions -- the fixed path finishes 30-100x under the deadline
    (0.01-0.03 s over three runs) and the broken one exceeds it 8.5x, timing
    out at 1.00 s on every run. A 30 s timeout at 32 MiB would also work but
    is slower and leaves the broken side only ~1.3x over, which is the tight
    threshold this avoids.

    The assertion is on COMPLETENESS, not on elapsed time, so a loaded CI box
    cannot fail it for the wrong reason.
    """
    size = 16 * 1024 * 1024
    payload = b"x" * size + END_OF_FILE_MARKER
    device, ser = _device_with(payload)
    got = device.sd_get_file_binary("log.bin", timeout=1.0)
    assert len(got) == size, (
        "the download returned %d of %d bytes; the scan did not keep up with "
        "the source (%d bytes still unread)" % (len(got), size, ser.unread)
    )
    assert ser.unread == 0, "%d bytes were left unread" % ser.unread


def test_the_marker_is_found_when_it_straddles_two_reads():
    """The incremental scan's correctness condition, not its speed.

    Searching only the NEW bytes would miss a marker split across two reads.
    The overlap of len(marker)-1 is what makes it exact, and this is the case
    that distinguishes a correct incremental scan from a fast broken one.
    """
    half = len(END_OF_FILE_MARKER) // 2
    # The chunk boundary lands INSIDE the marker.
    body = b"y" * (4096 - half)
    payload = body + END_OF_FILE_MARKER
    device, ser = _device_with(payload)
    got = device.sd_get_file_binary("log.bin", timeout=10.0)
    assert len(got) == len(body), (
        "a marker straddling the chunk boundary was missed: returned %d of "
        "%d payload bytes" % (len(got), len(body))
    )


# --------------------------------------------------------------------------
# :1027 -- the idle deadline reset before the work instead of after it
# --------------------------------------------------------------------------

class _VirtualClock:
    """A clock that moves only when told to. Stands in for `time` in device.py.

    The point is to make PROCESSING COST VIRTUAL TIME. A functional test of the
    reset ORDER needs the clock to advance between the reset and the next
    deadline check -- i.e. during the scan -- and doing that with real time
    needs a timeout on the order of scheduling noise, which is what made the
    first version of this leg flaky (see the AST leg below for the
    measurements). A virtual clock removes real time from the question
    entirely: no sleeps, and the verdict cannot depend on machine load.
    """

    def __init__(self, start: float = 1000.0):
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_the_idle_timeout_excludes_processing_measured_with_a_virtual_clock():
    """⛔ FAILS AT 5309e5ff: stops after one iteration with 61455 bytes unread.

    The loop's `time` is a virtual clock, and the preamble scan it calls on the
    way to the marker check is wrapped so each call costs 1.0 s of VIRTUAL
    time against a 0.5 s timeout. That makes the reset order decisive and
    nothing else:

      reset BEFORE the scan (r10): deadline = t0 + 0.5, then the scan moves the
        clock to t0 + 1.0, so the next `while` check is already past it and the
        download stops with most of the source unread.
      reset AFTER the scan (fixed): the scan moves the clock to t0 + 1.0 and
        THEN the deadline becomes t0 + 1.5, so the loop continues and the
        transfer completes.

    ⭐ THE CONTROL THAT MAKES THIS HONEST: the hook is called exactly ONCE in
    BOTH implementations, so the discrimination cannot be coming from the fixed
    version calling it less often (it caches the resolved span, the old one
    re-asked per iteration). Equal call counts, opposite outcomes, one cause --
    the order. Asserted below, not assumed.
    """
    import daqifi.device as device_mod
    from daqifi.sdcard import _PreambleSkip

    clock = _VirtualClock()
    real_time = device_mod.time
    real_head_span = _PreambleSkip.head_span
    calls = {"n": 0}

    def _costly_head_span(self, buf):
        calls["n"] += 1
        clock.advance(1.0)                 # processing, priced in virtual time
        return real_head_span(self, buf)

    device_mod.time = clock
    _PreambleSkip.head_span = _costly_head_span
    try:
        payload = b"q" * 65536 + END_OF_FILE_MARKER
        device, ser = _device_with(payload)
        got = device.sd_get_file("log.csv", timeout=0.5)
    finally:
        device_mod.time = real_time
        _PreambleSkip.head_span = real_head_span

    assert calls["n"] == 1, (
        "the hook fired %d times; this leg is only sound while both "
        "implementations call it the same number of times, otherwise it is "
        "measuring call frequency rather than the reset order" % calls["n"]
    )
    assert ser.unread == 0, (
        "the loop stopped with %d bytes still at the source, so the idle "
        "window was consumed by the host's own processing instead of "
        "measuring silence" % ser.unread
    )
    assert len(got) == 65536, (
        "captured %d of 65536 payload bytes" % len(got)
    )


def test_the_idle_deadline_is_reset_after_the_scan_not_before_it():
    """⛔ r10 :1027 (blind leg). Pinned STRUCTURALLY, and that is deliberate.

    The property: within the read loop, the idle-deadline reset must come
    AFTER the marker scan, so the host's own processing is excluded from a
    window that is documented to measure SILENCE. At r10 the reset sat inside
    the `if pending:` block before the copy and scan, so processing time was
    charged against the next window.

    ⚠ THE FUNCTIONAL FORM OF THIS TEST WAS MEASURED FLAKY AND IS NOT USED.
    Driving it needs a timeout on the order of ONE ITERATION's processing --
    about 1-2 ms at these sizes -- which is also the order of ordinary
    scheduling noise. Measured on r10 with 2 MiB at timeout=0.001: one run
    raised TimeoutError with 65551 bytes unread, and the very next run
    returned all 2097152 bytes with nothing unread. A test whose verdict flips
    on the same input and the same code is not a regression test; it is a coin
    flip that will eventually be "fixed" by someone deleting it.

    ⭐ So the order is asserted over the AST instead: deterministic, immune to
    load, and it names the property rather than a symptom of it. Fixing :1028
    shrinks the processing time but does NOT make this ordering correct -- the
    two findings are independent, which is exactly why this pin does not lean
    on the other one's test.
    """
    import ast
    import inspect

    from daqifi.device import NyquistDevice

    tree = ast.parse(inspect.getsource(NyquistDevice._read_sd_response).lstrip())
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.While)]
    assert len(loops) == 1, (
        "expected exactly one read loop, found %d -- this pin addresses the "
        "wrong code if the shape changed" % len(loops)
    )
    body = loops[0].body

    def _index_of(pred):
        for i, st in enumerate(body):
            if pred(ast.unparse(st)):
                return i
        return None

    # The reset is the statement that assigns the deadline from the clock.
    reset_at = _index_of(lambda s: "deadline = time.monotonic() + timeout" in s)
    # The scan is the statement that searches for the end-of-file marker.
    scan_at = _index_of(lambda s: "END_OF_FILE_MARKER" in s)

    assert reset_at is not None, "no idle-deadline reset found in the loop"
    assert scan_at is not None, "no marker scan found in the loop"
    assert scan_at < reset_at, (
        "the idle deadline is reset at loop-body index %d, BEFORE the marker "
        "scan at index %d, so the host's copy-and-scan time is charged "
        "against a window that is documented to measure silence"
        % (reset_at, scan_at)
    )


# --------------------------------------------------------------------------
# :808 -- a listing publish resurrecting sizes an invalidation dropped
# --------------------------------------------------------------------------

LISTING = b'DAQiFi/log.bin 3\r\n__END_OF_LIST__ OK\r\n'


class _PausingSerial(_ChunkedSerial):
    """Serves a listing, then BLOCKS until released.

    The pause sits after the whole response has been handed over, so the
    listing is fully parsed and about to publish when the invalidation lands.
    """

    def __init__(self, payload: bytes):
        _ChunkedSerial.__init__(self, payload, chunk=len(payload))
        self.served = threading.Event()
        self.release = threading.Event()

    def read(self, n: int = 1) -> bytes:
        out = _ChunkedSerial.read(self, n)
        if self.unread == 0 and out:
            self.served.set()
            self.release.wait(10.0)
        return out


def test_a_listing_that_crosses_an_invalidation_is_discarded() -> None:
    """⛔ FAILS AT 5309e5ff: sd_get_file_size answers 3 after disconnect().

    Thread A parses the listing and pauses before publishing. Thread B calls
    disconnect(), which clears the cache. A resumes and -- at 5309e5ff --
    writes the stale entry back, so the getter reports 3 for a device we have
    stopped talking to, where its documented miss value is 0.

    ⭐ `disconnect()` does not invalidate because the data is OLD. It
    invalidates because THE DEVICE'S STATE IS NO LONGER KNOWN, which is why
    the invalidation is authoritative and the listing is discarded. r10's
    comment claimed the opposite -- that the listing "holds strictly newer
    data" -- and that claim is what this test refutes.
    """
    device = NyquistDevice("COM41")
    ser = _PausingSerial(LISTING)
    device._comm._serial = ser

    result = {}

    def _list():
        try:
            result["files"] = device.sd_list_files()
        except Exception as exc:                              # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=_list, daemon=True)
    t.start()
    assert ser.served.wait(10.0), "the listing never reached the pause"
    # The invalidation lands while the listing is parked before its publish.
    device.disconnect()
    ser.release.set()
    t.join(10.0)
    assert not t.is_alive(), "the listing thread did not finish"

    assert device.sd_get_file_size("DAQiFi/log.bin") == 0, (
        "a listing that completed across an invalidation was published, so "
        "the getter resurrected a size for a disconnected device"
    )
    assert device._file_sizes == {}, (
        "the cache should be the empty one the invalidation left, not the "
        "listing's: %r" % (device._file_sizes,)
    )


def test_a_listing_with_no_invalidation_still_publishes() -> None:
    """⭐ THE POSITIVE CONTROL for the generation guard.

    Without this, the test above passes on an implementation that never
    publishes at all -- which would be "correct" and useless.
    """
    device = NyquistDevice("COM41")
    device._comm._serial = _ChunkedSerial(LISTING, chunk=len(LISTING))
    files = device.sd_list_files()
    assert files == ["DAQiFi/log.bin"]
    assert device.sd_get_file_size("DAQiFi/log.bin") == 3, (
        "an undisturbed listing must still populate the cache"
    )
