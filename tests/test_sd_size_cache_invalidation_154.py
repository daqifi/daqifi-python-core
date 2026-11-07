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

from daqifi.device import NyquistDevice
from daqifi.sdcard import END_OF_FILE_MARKER


class _Serial:
    """Replies by looking at the command, not by position.

    ⚠ An earlier version of this fake popped one reply per write, which
    broke as soon as a helper wrote twice: a setter received the listing's
    reply and raised ``CommandError: malformed error response``, so the
    tests failed for a fake-construction reason rather than for the defect
    they are about. Dispatching on the command text cannot drift that way.
    """

    def __init__(self, listing: bytes = b"", download: bytes = b"") -> None:
        self.is_open = True
        self.written: list = []
        self._listing = listing
        self._download = download
        self._pending = bytearray()

    def write(self, data) -> int:
        text = bytes(data)
        self.written.append(text)
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
        return len(self._pending)

    def read(self, n: int) -> bytes:
        out = bytes(self._pending[:n])
        del self._pending[:n]
        return out


LISTING = b'DAQiFi/log.bin 3\r\n__END_OF_LIST__ OK\r\n'
DOWNLOAD = b"abcdef" + END_OF_FILE_MARKER


def _device() -> NyquistDevice:
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(LISTING, DOWNLOAD)
    return device


def test_a_listing_then_more_logging_does_not_reject_the_download() -> None:
    """The audit's scenario end to end: list, enable logging, download."""
    device = _device()
    device.sd_list_files()
    assert device.sd_get_file_size("log.bin") == 3, "the listing did not cache"
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
    assert device.sd_get_file_size("log.bin") == 3
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


def test_a_path_prefixed_listing_is_found_by_the_bare_name() -> None:
    """⛔ THE OTHER HALF, and it failed SILENTLY. The listing stores what the
    device printed -- ``DAQiFi/log.bin`` -- while a download is asked for by
    bare name, so an exact dict lookup misses in the normal case. A miss
    means no size, and no size means the marker scan falls back to "first
    marker wins" -- so the discrimination this PR added never engaged on a
    real listing, and nothing failed to say so.
    """
    device = _device()
    device.sd_list_files()
    assert device.sd_get_file_size("log.bin") == 3, (
        "a path-prefixed listing was not found by the name a caller uses, "
        "so expected_size stays None and the sized path is unreachable"
    )


def test_an_ambiguous_basename_resolves_to_no_size() -> None:
    """Two listed files sharing a basename cannot say which is being
    fetched, and a boundary from the wrong file rejects a good download. The
    unsized path is the safe direction, so ambiguity means None."""
    device = _device()
    device._comm._serial = _Serial(
        b'DAQiFi/log.bin 3\r\nOTHER/log.bin 99\r\n__END_OF_LIST__ OK\r\n',
        DOWNLOAD,
    )
    device.sd_list_files()
    assert device.sd_get_file_size("log.bin") == 0, (
        "an ambiguous basename produced a size, so a download could be "
        "measured against the wrong file"
    )
    # And the download still works, unsized.
    assert device.sd_get_file_binary("log.bin", timeout=2.0) == b"abcdef"


def test_a_fresh_listing_restores_the_size() -> None:
    """Invalidation is not a one-way door: the next listing taken while not
    logging is a safe length again, which is what makes forgetting cheap.
    """
    device = _device()
    device.sd_list_files()
    device.sd_enable(True)
    assert device.sd_get_file_size("log.bin") == 0
    device.sd_list_files()
    assert device.sd_get_file_size("log.bin") == 3
