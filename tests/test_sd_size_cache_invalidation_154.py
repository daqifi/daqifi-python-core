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
LISTED = "DAQiFi/log.bin"
DOWNLOAD = b"abcdef" + END_OF_FILE_MARKER


def _device() -> NyquistDevice:
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(LISTING, DOWNLOAD)
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
