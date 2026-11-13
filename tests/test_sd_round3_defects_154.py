"""r2's defects that SURVIVE ruling FO A2, each introduced by this PR.

⚠ Four tests were removed from this file when FO A2 withdrew the legacy
path's size logic: the two that pinned the hint fallback in both stale
directions, the one that pinned the agreeing-size discrimination, and the one
asserting the fallback did not swallow a device error. All four described a
mechanism that no longer exists. What the removal left behind is pinned in
test_sd_legacy_path_is_unsized_154.py instead.

What remains here are the fixes FO A2 keeps: the eighth SD writer's
invalidation hook, the preamble flush, and the per-path timeout semantics.
"""

import io
import time

import pytest

from _sd_echo_probe import EchoProbeFake
from daqifi._internal import scpi_commands
from daqifi.device import NyquistDevice
from daqifi.sdcard import END_OF_FILE_MARKER, SdCardFileReceiver
from daqifi.streaming import StreamManager

PROMPT = b"DAQIFI> "


class _Serial(EchoProbeFake):
    def __init__(self, listing: bytes = b"", download: bytes = b"") -> None:
        self.is_open = True
        self.written: list = []
        self._listing = listing
        self._download = download
        self._pending = bytearray()

    def write(self, data) -> int:
        text = bytes(data)
        self.written.append(text)
        if self._probe_write(text):
            return len(data)
        if b"SD:LIST?" in text:
            self._pending.extend(self._listing)
        elif b"SD:GET" in text:
            self._pending.extend(self._download)
        else:
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


class _Trickle(EchoProbeFake):
    """Hands out one chunk per read with a gap, so a total duration longer
    than the timeout is reached without any single gap approaching it."""

    def __init__(self, payload: bytes, chunk: int = 4, gap: float = 0.05):
        self.is_open = True
        self._data = bytearray(payload)
        self._chunk = chunk
        self._gap = gap
        self._last = 0.0

    def write(self, data) -> int:
        self._probe_write(data)
        return len(data)

    def flush(self) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        def _own():
            if not self._data:
                return 0
            if time.monotonic() - self._last < self._gap:
                return 0
            return min(self._chunk, len(self._data))
        return self._probe_pending(_own)

    def read(self, n: int) -> bytes:
        def _own():
            self._last = time.monotonic()
            out = bytes(self._data[:n])
            del self._data[:n]
            return out
        return self._probe_read(n, _own)


def _device(listing: bytes = b"", download: bytes = b"") -> NyquistDevice:
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(listing, download)
    return device


# -- 1: streaming is the eighth SD writer ----------------------------------


def test_create_stream_wires_the_invalidation_hook() -> None:
    """⛔ THE WRITER AN sd_-NAMED ENUMERATION COULD NOT SEE. StreamManager
    records to the configured SD file and has no `sd_` in its name, so the
    grep that produced "every writer enumerated" was blind to it. It holds
    the transport, not the device, hence a callback.
    """
    from daqifi.discovery import BoardVariant

    device = _device()
    # create_stream refuses an unknown variant, so give it a real one.
    device._variant = BoardVariant.NQ1
    device._connected = True
    device._initialize_channels()
    stream = device.create_stream(channels=[0], sample_rate=100)
    assert stream._on_recording_change == device._forget_sd_file_sizes


@pytest.mark.parametrize("method", ["start", "stop"])
def test_a_recording_boundary_forgets_the_cached_sizes(method) -> None:
    """Both ends of a recording change what is on the card: start begins
    appending, stop closes a file that grew."""
    fired = []
    manager = StreamManager(
        _Serial(), [0], 100, on_recording_change=lambda: fired.append(method),
    )
    try:
        getattr(manager, method)()
    except Exception:                                         # noqa: BLE001
        # The fake transport cannot complete a real start/stop; the hook
        # fires before any of that, which is the point.
        pass
    assert fired == [method], (
        "a recording boundary did not invalidate, so a listing taken before "
        "it stays enforced afterwards"
    )


# -- 4: the preamble skip lost bytes it was holding ------------------------


def test_a_payload_that_is_a_prefix_of_the_head_is_not_lost_at_eof() -> None:
    """⛔ codex's input. The whole stream is a proper PREFIX of a head
    candidate, so the skip held it waiting for a longer match that never
    came, and at EOF the receiver reported a TIMEOUT for bytes it was
    holding. Flushing at EOF is the only honest answer.
    """
    name = "__END_OF_FILE__"
    cmd = scpi_commands.get_sd_file(name).data.encode("ascii")
    stream = b'SYSTem:STORage:SD:GET "__END_OF_FILE__'
    assert cmd.startswith(stream), "this input must be a proper prefix of the echo"
    # No complete candidate matches a mere prefix, so nothing is stripped --
    # and these bytes are then a legitimate 23-byte file followed by the EOF
    # marker, which is what the flush lets the receiver see.
    out = io.BytesIO()
    written = SdCardFileReceiver(io.BytesIO(stream)).receive(
        out, name, timeout_s=2.0, expected_size=23, preamble=cmd,
    )
    assert out.getvalue() == b'SYSTem:STORage:SD:GET "'
    assert written == 23
    # On f96a59ae this raised: "Transport stream closed before receiving the
    # EOF marker. Received 0 bytes." -- reporting nothing received while
    # holding the entire payload.


# -- 5: the text path's timeout measured the wrong thing -------------------


def test_a_slow_but_progressing_text_download_is_not_cut_off() -> None:
    """⛔ The base reset its clock on every chunk, so the text path's
    timeout measured SILENCE. Delegating made it absolute, which kills any
    text download longer than the timeout however healthy it is.
    """
    payload = b"x" * 240 + END_OF_FILE_MARKER
    device = NyquistDevice("COM41")
    device._comm._serial = _Trickle(payload, chunk=4, gap=0.02)
    started = time.monotonic()
    got = device.sd_get_file("log.csv", timeout=0.5)
    elapsed = time.monotonic() - started
    assert got == "x" * 240
    assert elapsed > 0.5, (
        "the download finished faster than the timeout, so this did not "
        "exercise a transfer longer than it"
    )


def test_the_binary_paths_timeout_stays_absolute() -> None:
    """The binary sibling was absolute in the base and stays absolute: the
    two paths are not being silently unified."""
    payload = b"x" * 240 + END_OF_FILE_MARKER
    device = NyquistDevice("COM41")
    device._comm._serial = _Trickle(payload, chunk=4, gap=0.02)
    with pytest.raises(TimeoutError):
        device.sd_get_file_binary("log.bin", timeout=0.5)


# -- the hint: a disagreeing size must not reject a valid download ---------


def test_a_file_NAMED_for_a_marker_is_downloaded_whole_with_echo_on() -> None:
    """⛔ MY OWN FIFTH INSTANCE, caught by grepping my diff for "cannot".

    A GET names a file, and a file can be named `__END_OF_FILE__`, so the
    echoed command contains that marker's bytes. A capture that scans the
    whole buffer stops on the echo and loses the payload -- the same defect
    as the `__TRANSFER_ERROR__.bin` pin, one marker over. The scan starts
    after the echo.
    """
    name = "__END_OF_FILE__"
    echo = scpi_commands.get_sd_file(name).data.encode("ascii")
    device = NyquistDevice("COM41")
    device._comm._serial = _Serial(
        b"", echo + b"\r\n" + PROMPT + b"payload" + END_OF_FILE_MARKER,
    )
    assert END_OF_FILE_MARKER in echo, "this name must put the marker in the echo"
    assert device.sd_get_file_binary(name, timeout=2.0) == b"payload"
