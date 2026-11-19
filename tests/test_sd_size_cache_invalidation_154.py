"""r1 MEDIUM: making the download size-dependent made a stale cache fatal.

⛔ INTRODUCED BY THIS PR, not pre-existing. The base ignored
``_file_sizes`` entirely, so a stale entry cost nothing. Delegating to
``SdCardFileReceiver`` with ``expected_size`` turned the cached number into
an enforced boundary: a listing taken before more data was logged now
rejects a perfectly good download with ``OSError``.

The scenario, which is the audit's:

1. ``sd_list_files()`` caches ``log.bin`` at 3 bytes.
2. SD logging is enabled and more data is written; nothing invalidates it.
3. ``sd_get_file_binary()`` enforces the stale 3-byte boundary and refuses
   a valid ``b"abcdef__END_OF_FILE__"``.

⭐ THE COSTS ARE ASYMMETRIC, which is why every writer invalidates rather
than trying to track what changed. Over-invalidating loses only the
marker-in-content discrimination, degrading to the unsized path the base
always used; under-invalidating raises on valid data. So anything that can
change file contents or existence forgets the sizes.

Mirrors ``DaqifiStreamingDevice._forget_sd_card_sizes``, which already does
this for the modern path, including on connect and disconnect because a
listing from an earlier session is not a safe download length either.
"""

import pytest

from _sd_echo_setter import (EchoSetterFake,
                             transport_cannot_silence)
from daqifi.device import NyquistDevice
from daqifi.sdcard import END_OF_FILE_MARKER


class _Serial(EchoSetterFake):
    """Replies by looking at the command, not by position.

    ⚠ An earlier version of this fake popped one reply per write, which
    broke as soon as a helper wrote twice: a setter received the listing's
    reply and raised ``CommandError: malformed error response``, so the
    tests failed for a fake-construction reason rather than for the defect
    they are about. Dispatching on the command text cannot drift that way.
    """
    out_waiting = 0  # #263: the send path drains this queue, bounded

    def __init__(self, listing: bytes = b"", download: bytes = b"") -> None:
        self.is_open = True
        self.written: list = []
        self._listing = listing
        self._download = download
        self._pending = bytearray()

    def write(self, data) -> int:
        text = bytes(data)
        self.written.append(text)
        # #260: the probe's reply comes from the shared policy, not from this
        # fake's command dispatch -- see tests/_sd_echo_probe.py.
        if self._console_write(text):
            return len(data)
        if b"SD:LIST?" in text or b"STORage:SD:LIST?" in text:
            self._pending.extend(self._listing)
        elif b"SD:GET" in text:
            self._pending.extend(self._download)
        else:
            # Setters are verified by a following error-queue read.
            self._pending.extend(b'0,"No error"\r\n')
        return len(data)

    def flush(self) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        return self._console_pending(lambda: len(self._pending))

    def read(self, n: int) -> bytes:
        def _own():
            out = bytes(self._pending[:n])
            del self._pending[:n]
            return out
        return self._console_read(n, _own)


LISTING = b'DAQiFi/log.bin 3\r\n__END_OF_LIST__ OK\r\n'
LISTED = "DAQiFi/log.bin"
DOWNLOAD = b"abcdef" + END_OF_FILE_MARKER


def _device() -> NyquistDevice:
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(LISTING, DOWNLOAD)
    transport_cannot_silence(device)
    return device


def test_a_listing_then_more_logging_does_not_reject_the_download() -> None:
    """The audit's scenario end to end: list, enable logging, download."""
    device = _device()
    device.sd_list_files()
    assert device.sd_get_file_size(LISTED) == 3, "the listing did not cache"
    device.sd_enable(True)
    assert device.sd_get_file_binary("log.bin", timeout=2.0) == b"abcdef", (
        "a stale listed size rejected a valid download"
    )


@pytest.mark.parametrize(
    "writer",
    [
        pytest.param(lambda d: d.sd_enable(True), id="sd_enable(True)"),
        pytest.param(lambda d: d.sd_enable(False), id="sd_enable(False)"),
        pytest.param(lambda d: d.sd_set_logging("log.bin"), id="sd_set_logging"),
        pytest.param(lambda d: d.sd_delete_file("other.bin"), id="sd_delete_file"),
        pytest.param(lambda d: d.sd_format(), id="sd_format"),
        pytest.param(lambda d: d.sd_benchmark(1), id="sd_benchmark"),
        pytest.param(lambda d: d.disconnect(), id="disconnect"),
    ],
)
def test_every_writer_forgets_the_cached_sizes(writer) -> None:
    """⛔ ENUMERATED, not sampled. Each of these can change what is on the
    card -- or, for disconnect, ends the session the listing belonged to --
    so each must drop the cache. A writer added later without this is the
    way the defect comes back.
    """
    device = _device()
    device.sd_list_files()
    assert device.sd_get_file_size(LISTED) == 3
    try:
        writer(device)
    except Exception:                                         # noqa: BLE001
        # The transport fake answers nothing useful to a setter; what is
        # under test is the cache, not the command's own error handling.
        pass
    assert device.sd_get_file_size("log.bin") == 0, (
        "the cached size survived an operation that can change the file, so "
        "a later download would enforce a boundary that no longer holds"
    )


def test_a_size_is_never_borrowed_across_directories() -> None:
    """r2 MED: an earlier revision matched on the final path segment, so a
    size listed for OTHER/log.bin became the enforced boundary for a
    download of DAQiFi/log.bin. The device resolves a bare GET in its OWN
    default directory, not in whichever one was last listed, so neither a
    join nor a basename match can be made safe. Exact key only.
    """
    device = _device()
    device._comm._serial = _Serial(
        b"OTHER/log.bin 3\r\n__END_OF_LIST__ OK\r\n", DOWNLOAD,
    )
    transport_cannot_silence(device)
    device.sd_list_files()
    assert device.sd_get_file_size("DAQiFi/log.bin") == 0
    assert device.sd_get_file_size("log.bin") == 0, (
        "a bare name borrowed a size from a listed directory"
    )
    # 6 bytes against a borrowed 3-byte boundary used to raise OSError.
    assert device.sd_get_file_binary("log.bin", timeout=2.0) == b"abcdef"


def test_a_listed_zero_is_an_empty_file_not_an_unknown_size() -> None:
    """r2 MED: `if size:` sent a listed 0 down the unsized path, and in the
    basename revision let it borrow another entry's size. A listed 0 is a
    real empty file; the OLD format's "no size given" is stored as None, so
    the two are distinguishable and neither is guessed.
    """
    device = _device()
    device._comm._serial = _Serial(
        b"DAQiFi/empty.bin 0\r\nOTHER/empty.bin 3\r\n__END_OF_LIST__ OK\r\n",
        END_OF_FILE_MARKER,
    )
    transport_cannot_silence(device)
    device.sd_list_files()
    assert device._listed_size_for("DAQiFi/empty.bin") == 0
    assert device.sd_get_file_binary("DAQiFi/empty.bin", timeout=2.0) == b""


def test_an_unsized_old_format_listing_enforces_no_boundary() -> None:
    """The old listing format gives no size. Stored as None the download
    runs unsized; stored as 0 it would enforce a zero-byte boundary and
    reject every transfer of that file."""
    device = _device()
    device._comm._serial = _Serial(
        b"DAQiFi/log.bin\r\n__END_OF_LIST__ OK\r\n", DOWNLOAD,
    )
    transport_cannot_silence(device)
    device.sd_list_files()
    assert device._listed_size_for("DAQiFi/log.bin") is None
    assert device.sd_get_file_binary("DAQiFi/log.bin", timeout=2.0) == b"abcdef"


def test_a_fresh_listing_restores_the_size() -> None:
    """Invalidation is not a one-way door: the next listing taken while not
    logging is a safe length again, which is what makes forgetting cheap.
    """
    device = _device()
    device.sd_list_files()
    device.sd_enable(True)
    assert device.sd_get_file_size(LISTED) == 0
    device.sd_list_files()
    assert device.sd_get_file_size(LISTED) == 3


# ----------------------------------------------------------------------------
# r9 :830 (MEDIUM): the size lookup must be ATOMIC against invalidation.
#
# `_forget_sd_file_sizes` REPLACES `device._file_sizes` and is wired to
# StreamManager's recording-change callback, which runs on ANOTHER THREAD --
# AsyncStreamManager.stop() in particular. The lookup used to read the
# attribute TWICE, `if filename not in self._file_sizes` and then
# `self._file_sizes[filename]`, so an invalidation landing between them left
# the index hitting a fresh empty dict: KeyError out of a getter whose
# contract is to return a number. The base did one `dict.get()`, so the window
# was INTRODUCED by splitting the lookup.
#
# ⭐ NO THREADS AND NO SLEEPS IN THESE TESTS. A real race would be
# nondeterministic and would pass by luck on a good day. Instead the dict
# itself performs the invalidation, from inside the very operation the old code
# used as its check -- which makes the interleaving exact and repeatable.
# ----------------------------------------------------------------------------

class _SwapOnContains(dict):
    """A cache that invalidates itself the moment someone tests membership.

    This stands in for "another thread replaced the attribute between the
    check and the index". `__contains__` is the old code's check, so the swap
    happens exactly in the window under test -- and the fixed code never calls
    it, which is the property being pinned.
    """

    def __init__(self, *a, **kw):
        dict.__init__(self, *a, **kw)
        self.device = None
        self.contains_calls = 0

    def __contains__(self, key):
        self.contains_calls += 1
        if self.device is not None:
            self.device._file_sizes = {}        # the concurrent invalidation
        return dict.__contains__(self, key)


def test_the_size_lookup_survives_invalidation_between_check_and_access() -> None:
    """⛔ FAILS AT a07b4886 with KeyError: 'DAQiFi/log.bin'."""
    device = _device()
    device.sd_list_files()
    assert device.sd_get_file_size(LISTED) == 3, "the listing did not cache"

    hostile = _SwapOnContains(device._file_sizes)
    hostile.device = device
    device._file_sizes = hostile

    # The old code: `in` fires the swap, then the index hits the new empty
    # dict and raises. The fixed code does one `.get()` on the object it
    # already holds and answers from it.
    assert device.sd_get_file_size(LISTED) == 3, (
        "an invalidation racing the lookup changed the answer"
    )
    assert hostile.contains_calls == 0, (
        "the lookup tested membership before reading, which is the "
        "check-then-index window r9 :830 closed -- %d call(s)"
        % hostile.contains_calls
    )


class _SwapOnGet(dict):
    """A cache that invalidates itself from inside `get` itself.

    The stronger form: even when the invalidation lands during the ONE
    operation the fix relies on, the answer still comes from the object the
    caller had already resolved. This is what "one operation on one object"
    buys, and it holds whether the clear REPLACES the attribute or calls
    `.clear()` on it.
    """

    def __init__(self, *a, **kw):
        dict.__init__(self, *a, **kw)
        self.device = None

    def get(self, key, *a):
        if self.device is not None:
            self.device._file_sizes = {}
        return dict.get(self, key, *a)


def test_the_size_lookup_answers_from_the_dict_it_resolved() -> None:
    device = _device()
    device.sd_list_files()
    hostile = _SwapOnGet(device._file_sizes)
    hostile.device = device
    device._file_sizes = hostile
    assert device.sd_get_file_size(LISTED) == 3, (
        "the lookup re-read the attribute after resolving it"
    )
    # ⭐ The positive control for the swap itself: it really did fire, so the
    # assertion above is not passing because nothing happened.
    assert device._file_sizes == {}, (
        "the simulated invalidation never ran, so this test proves nothing"
    )


def test_a_listing_publishes_its_cache_in_one_step() -> None:
    """An invalidation DURING a listing leaves the cache whole, not partial.

    The listing used to assign `self._file_sizes = {}` and then write into the
    attribute once per line. An invalidation part-way through split the writes
    across two dicts and left a HALF POPULATED cache -- a wrong size from the
    getter rather than a crash, so nothing failed and nobody noticed.

    ⚠ TWO THINGS HERE ARE LOAD-BEARING, both measured after a version of this
    test passed on the UNFIXED code:

      * THREE files, not the module's one-file LISTING. With a single entry a
        mid-listing invalidation cannot produce a state distinguishable from
        "empty" or "complete". Three, split after the first, make the partial
        cache {b, c}: not empty, not complete, detectable.
      * THE TRIGGER IS CONTENT-BASED, and it took two wrong tries to get
        there. `sd_list_files` runs the helper over the lines TWICE --
        measured, 8 calls for 4 lines -- and the first pass caches nothing, so
        firing on "call 2" landed in that first pass and changed nothing:
        the test passed on the defect. Firing instead on "the cache looks
        partly built" then failed on the FIXED code, because the fix keeps the
        attribute EMPTY for the whole loop, so that trigger can never fire --
        the property being tested makes a state-based trigger unobservable.
        Firing on the SECOND sighting of the middle file's line works on both
        versions: it is inside the caching pass, after the first entry and
        before the last, and it depends on nothing the fix changes.
    """
    # Patched on daqifi.sdcard, not on daqifi.device: sd_list_files imports
    # the helper INSIDE the function (`from .sdcard import _list_end_status`),
    # so the name resolves from the sdcard module at call time.
    import daqifi.sdcard as sdcard_mod

    three = (b"DAQiFi/a.bin 1\r\n"
             b"DAQiFi/b.bin 2\r\n"
             b"DAQiFi/c.bin 3\r\n"
             b"__END_OF_LIST__ OK\r\n")
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(three, DOWNLOAD)
    transport_cannot_silence(device)

    calls = {"n": 0, "b": 0, "fired": False}
    real = sdcard_mod._list_end_status

    def _invalidate_midway(line):
        calls["n"] += 1
        if line.strip().startswith("DAQiFi/b.bin"):
            calls["b"] += 1
            # The SECOND sighting is the caching pass: a.bin has been recorded
            # by then and b.bin / c.bin have not.
            if calls["b"] == 2 and not calls["fired"]:
                calls["fired"] = True
                device._forget_sd_file_sizes()
        return real(line)

    sdcard_mod._list_end_status = _invalidate_midway
    try:
        files = device.sd_list_files()
    finally:
        sdcard_mod._list_end_status = real

    assert len(files) == 3, "the listing itself did not parse three files"
    # ⭐ THE POSITIVE CONTROL: the invalidation must really have fired, and
    # fired while the cache was PARTLY built. Without this the assertion below
    # passes whenever the hook did nothing -- which is how the first version
    # of this test passed on the defect.
    assert calls["fired"], "the invalidation never fired, so this proves nothing"
    assert calls["b"] >= 2, (
        "the middle file's line was seen %d time(s); the caching pass was "
        "never reached, so the invalidation did not land mid-build"
        % calls["b"]
    )
    cached = dict(device._file_sizes)
    # Either the listing won (whole) or the invalidation won (empty). A
    # partially populated cache is the defect and is neither.
    assert cached == {} or set(cached) == set(files), (
        "the cache is neither empty nor complete -- it was published "
        "incrementally and an invalidation split it: %r vs listed %r"
        % (cached, files)
    )


# ----------------------------------------------------------------------------
# r11 :835 (MAIN + BLIND): the compare and the publish must be ONE step.
#
# r11 fixed :830's check-then-index and then introduced another check-then-act
# in the fix itself: `if gen == captured: self._file_sizes = sizes`. An
# invalidation landing BETWEEN the comparison and the assignment publishes
# stale sizes anyway, so sd_get_file_size answers 3 after disconnect().
#
# ⭐ PINNED WITH A LINE TRACE, not a sleep. sys.settrace fires per line, so the
# pause lands exactly between the comparison and the assignment -- the window
# is entered deterministically rather than hoped for. On 3.9 the same window is
# reachable through ordinary bytecode switching; the trace just makes it
# certain on every run and every build.
# ----------------------------------------------------------------------------

def test_the_generation_check_and_the_publish_are_one_step() -> None:
    """⛔ FAILS AT a464b08e: sd_get_file_size answers 3 after disconnect().

    The trace pauses the listing thread on the line that assigns
    `self._file_sizes`, i.e. after the generation comparison has passed. A
    THIRD thread then runs the invalidation.

    ⚠ THE INVALIDATION MUST NOT RUN ON THE THREAD THAT RELEASES THE PAUSE, and
    the first version of this test did exactly that. With the fix in place the
    pause lands INSIDE the lock, so `disconnect()` on the releasing thread
    blocked on that lock while the paused thread waited to be released --
    a deadlock, broken only by the 10 s timeout, after which the publish and
    the clear happened in order and the assertions passed. The test was GREEN
    FOR THE WRONG REASON: not because the lock serialised anything, but
    because the timeout expired. The 10.19 s runtime was the only visible
    symptom.

    ⭐ Running the invalidation on its own thread makes both outcomes mean
    something. Fixed: it BLOCKS on the lock until the publish completes, then
    clears -- serialisation, observed. Broken: no lock, so it bumps and clears
    immediately and the parked publish then overwrites it with stale sizes.
    """
    import sys
    import threading

    import daqifi.device as device_mod

    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(LISTING, DOWNLOAD)
    transport_cannot_silence(device)

    target_file = device_mod.__file__
    paused = threading.Event()
    release = threading.Event()
    armed = {"on": True}

    def _source_line(frame):
        import linecache
        return linecache.getline(frame.f_code.co_filename, frame.f_lineno)

    def _tracer(frame, event, arg):
        if (event == "line" and armed["on"]
                and frame.f_code.co_filename == target_file
                and frame.f_code.co_name == "sd_list_files"
                and "self._file_sizes = sizes" in _source_line(frame)):
            armed["on"] = False
            paused.set()
            release.wait(10.0)
        return _tracer

    result = {}

    def _list():
        sys.settrace(_tracer)
        try:
            result["files"] = device.sd_list_files()
        except Exception as exc:                              # noqa: BLE001
            result["error"] = exc
        finally:
            sys.settrace(None)

    invalidated = threading.Event()

    def _invalidate():
        # On the fixed code this BLOCKS on the lock the paused thread holds.
        device.disconnect()
        invalidated.set()

    threading.settrace(_tracer)
    t = threading.Thread(target=_list, daemon=True)
    t.start()
    try:
        reached = paused.wait(10.0)
    finally:
        threading.settrace(None)

    if not reached:
        release.set()
        t.join(10.0)
        raise AssertionError(
            "the trace never paused on the publish line, so the race window "
            "was not entered and this test asserted nothing"
        )

    inv = threading.Thread(target=_invalidate, daemon=True)
    inv.start()
    # Give the invalidation a moment to either take effect (broken) or park on
    # the lock (fixed). Bounded and small: it is not timing the race, only
    # letting the other thread reach its blocking point.
    invalidated.wait(0.5)
    release.set()
    t.join(10.0)
    inv.join(10.0)
    assert not t.is_alive(), "the listing thread did not finish"
    assert not inv.is_alive(), "the invalidation thread did not finish"

    assert device.sd_get_file_size("DAQiFi/log.bin") == 0, (
        "an invalidation that landed between the generation check and the "
        "publish was overwritten, so the getter resurrected a size for a "
        "disconnected device"
    )
    assert device._file_sizes == {}, (
        "the cache should be the empty one the invalidation left: %r"
        % (device._file_sizes,)
    )
