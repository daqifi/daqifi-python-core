"""Tests for SdCardFileReceiver (closes #7).

Pure-Python stream scanner — accumulates source bytes into a
destination, detecting the ``__END_OF_FILE__`` marker that signals
firmware transfer completion. Marker can span chunk boundaries
(verified). No hardware required.
"""
from __future__ import annotations

import io
import time

import pytest

from daqifi.sdcard import (
    END_OF_FILE_MARKER,
    SdCardFileReceiver,
    SdCardTransferProgress,
    _find_eof_marker,
)


pytestmark = pytest.mark.portomatic_generated


# ---- helper streams --------------------------------------------------------


class _ChunkedStream:
    """Source stream that returns predetermined chunks per ``read``
    call. After the chunks are exhausted, returns ``b""`` (simulating
    a closed stream). Lets us drive boundary cases deterministically.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self._idx = 0

    def read(self, _n: int) -> bytes:
        if self._idx >= len(self._chunks):
            return b""
        out = self._chunks[self._idx]
        self._idx += 1
        return out


class _DelayedStream(_ChunkedStream):
    """Like _ChunkedStream but optionally exposes ``in_waiting`` for
    the receiver's "empty read but bytes pending" branch."""

    def __init__(self, chunks: list[bytes], in_waiting_seq: list[int]) -> None:
        super().__init__(chunks)
        self._in_waiting_seq = list(in_waiting_seq)
        self._iw_idx = 0

    @property
    def in_waiting(self) -> int:
        if self._iw_idx >= len(self._in_waiting_seq):
            return 0
        v = self._in_waiting_seq[self._iw_idx]
        self._iw_idx += 1
        return v


# ---- _find_eof_marker -----------------------------------------------------


class TestFindEofMarker:
    def test_marker_at_start_of_chunk(self):
        found, pos = _find_eof_marker(b"", END_OF_FILE_MARKER + b"trail")
        assert found is True
        assert pos == 0

    def test_marker_in_middle_of_chunk(self):
        chunk = b"hello" + END_OF_FILE_MARKER + b"world"
        found, pos = _find_eof_marker(b"", chunk)
        assert found is True
        assert pos == 5

    def test_marker_split_across_boundary(self):
        # Trailing has the first 5 bytes; chunk has the rest + payload.
        marker_head = END_OF_FILE_MARKER[:5]
        marker_tail = END_OF_FILE_MARKER[5:]
        found, pos = _find_eof_marker(marker_head, marker_tail + b"after")
        assert found is True
        assert pos == 0

    def test_no_marker_returns_false(self):
        found, pos = _find_eof_marker(b"abcde", b"fghij")
        assert found is False
        assert pos == -1

    def test_combined_too_short_returns_false(self):
        found, pos = _find_eof_marker(b"a", b"b")
        assert found is False
        assert pos == -1


# ---- SdCardFileReceiver — happy paths -------------------------------------


class TestReceiveSingleChunk:
    def test_receives_payload_then_marker_in_one_chunk(self):
        payload = b"hello world this is the payload"
        source = _ChunkedStream([payload + END_OF_FILE_MARKER])
        dest = io.BytesIO()

        receiver = SdCardFileReceiver(source, buffer_size=1024)
        n = receiver.receive(dest, "log.bin", timeout_s=2.0)

        assert n == len(payload)
        assert dest.getvalue() == payload

    def test_marker_only_no_payload(self):
        source = _ChunkedStream([END_OF_FILE_MARKER])
        dest = io.BytesIO()
        n = SdCardFileReceiver(source).receive(dest, "log.bin", timeout_s=2.0)
        assert n == 0
        assert dest.getvalue() == b""


class TestReceiveMultipleChunks:
    def test_payload_split_across_chunks_then_marker(self):
        chunks = [b"first-half-of-payload", b"second-half", END_OF_FILE_MARKER]
        source = _ChunkedStream(chunks)
        dest = io.BytesIO()

        n = SdCardFileReceiver(source).receive(dest, "log.bin", timeout_s=2.0)

        expected = b"first-half-of-payload" + b"second-half"
        assert n == len(expected)
        assert dest.getvalue() == expected

    def test_marker_split_across_two_chunks(self):
        # Chunk 1: payload + first 5 bytes of marker.
        # Chunk 2: rest of marker + nothing (marker terminates).
        chunks = [
            b"actual-payload" + END_OF_FILE_MARKER[:5],
            END_OF_FILE_MARKER[5:],
        ]
        source = _ChunkedStream(chunks)
        dest = io.BytesIO()

        n = SdCardFileReceiver(source).receive(dest, "log.bin", timeout_s=2.0)

        assert n == len(b"actual-payload")
        assert dest.getvalue() == b"actual-payload"

    def test_marker_split_across_three_chunks(self):
        # Marker spans 3 reads; trailing buffer must keep enough state.
        chunks = [
            b"payload-data" + END_OF_FILE_MARKER[:3],
            END_OF_FILE_MARKER[3:8],
            END_OF_FILE_MARKER[8:],
        ]
        source = _ChunkedStream(chunks)
        dest = io.BytesIO()

        n = SdCardFileReceiver(source).receive(dest, "log.bin", timeout_s=2.0)

        assert n == len(b"payload-data")
        assert dest.getvalue() == b"payload-data"

    def test_payload_bigger_than_buffer(self):
        # Buffer is 16 bytes; payload is 200 bytes; marker fits at end.
        payload = b"x" * 200
        chunks = []
        # Simulate the source returning 16-byte chunks.
        for i in range(0, len(payload), 16):
            chunks.append(payload[i:i + 16])
        chunks.append(END_OF_FILE_MARKER)
        source = _ChunkedStream(chunks)
        dest = io.BytesIO()

        n = SdCardFileReceiver(source, buffer_size=16).receive(
            dest, "log.bin", timeout_s=2.0
        )

        assert n == 200
        assert dest.getvalue() == payload


# ---- SdCardFileReceiver — error paths -------------------------------------


class TestReceiveErrorPaths:
    def test_stream_closed_before_marker_raises_timeout(self):
        # Source closes (returns b"") before marker arrives.
        source = _ChunkedStream([b"some-data"])
        dest = io.BytesIO()

        with pytest.raises(TimeoutError, match="closed before"):
            SdCardFileReceiver(source).receive(dest, "log.bin", timeout_s=2.0)

    def test_deadline_exceeded_raises_timeout_when_in_waiting_idle(self):
        """A pyserial-style source that reports in_waiting==0 forever
        (open but no bytes incoming) should hit the deadline and
        raise TimeoutError("timed out") rather than block forever
        or false-claim the stream is closed."""
        # ~25 entries comfortably covers a 0.2s timeout at 0.01s/poll.
        # Avoid allocating a huge list — the receiver only polls a
        # handful of times before the deadline trips.
        source = _DelayedStream(
            chunks=[],  # never used — in_waiting is always 0 so read isn't called
            in_waiting_seq=[0] * 64,  # always idle for the polling window
        )
        dest = io.BytesIO()

        start = time.monotonic()
        with pytest.raises(TimeoutError, match="timed out"):
            SdCardFileReceiver(source).receive(dest, "log.bin", timeout_s=0.2)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0

    def test_pyserial_idle_then_data_arrives(self):
        """Reception must not abort on transient empty reads — the
        common pyserial 'in_waiting==0 for a few polls then bytes
        arrive' pattern."""
        # Simulate: 3 idle polls (in_waiting=0), then 1 chunk + marker
        # arrives in one chunk.
        payload = b"finally-arrived"

        class _PyserialLike:
            def __init__(self):
                self._poll = 0
                # in_waiting=0 for the first 3 polls, then matches the chunk size.
                self._iw_sequence = [0, 0, 0, len(payload + END_OF_FILE_MARKER)]
                self._chunk = payload + END_OF_FILE_MARKER
                self._read_done = False

            @property
            def in_waiting(self) -> int:
                if self._poll < len(self._iw_sequence):
                    v = self._iw_sequence[self._poll]
                    self._poll += 1
                    return v
                return 0

            def read(self, _n: int) -> bytes:
                if self._read_done:
                    return b""
                self._read_done = True
                return self._chunk

        source = _PyserialLike()
        dest = io.BytesIO()
        n = SdCardFileReceiver(source).receive(dest, "log.bin", timeout_s=2.0)
        assert n == len(payload)
        assert dest.getvalue() == payload

    def test_rejects_none_destination(self):
        source = _ChunkedStream([END_OF_FILE_MARKER])
        with pytest.raises(TypeError, match="destination_stream"):
            SdCardFileReceiver(source).receive(None, "log.bin")  # type: ignore[arg-type]

    def test_rejects_none_file_name(self):
        source = _ChunkedStream([END_OF_FILE_MARKER])
        with pytest.raises(TypeError, match="file_name"):
            SdCardFileReceiver(source).receive(io.BytesIO(), None)  # type: ignore[arg-type]


class TestConstructorValidation:
    def test_rejects_none_source_stream(self):
        with pytest.raises(TypeError, match="source_stream"):
            SdCardFileReceiver(None)  # type: ignore[arg-type]

    def test_rejects_zero_buffer_size(self):
        with pytest.raises(ValueError, match="greater than zero"):
            SdCardFileReceiver(io.BytesIO(b""), buffer_size=0)

    def test_rejects_negative_buffer_size(self):
        with pytest.raises(ValueError, match="greater than zero"):
            SdCardFileReceiver(io.BytesIO(b""), buffer_size=-1)


class TestTimeoutValidation:
    def test_rejects_zero_timeout(self):
        source = _ChunkedStream([END_OF_FILE_MARKER])
        with pytest.raises(ValueError, match="positive finite"):
            SdCardFileReceiver(source).receive(
                io.BytesIO(), "log.bin", timeout_s=0
            )

    def test_rejects_negative_timeout(self):
        source = _ChunkedStream([END_OF_FILE_MARKER])
        with pytest.raises(ValueError, match="positive finite"):
            SdCardFileReceiver(source).receive(
                io.BytesIO(), "log.bin", timeout_s=-1.0
            )

    def test_rejects_nan_timeout(self):
        """``float('nan')`` slips past ``<= 0`` (NaN comparisons are
        always False) and would make the deadline check never fire,
        hanging the receiver forever. Must raise ValueError."""
        source = _ChunkedStream([END_OF_FILE_MARKER])
        with pytest.raises(ValueError, match="finite"):
            SdCardFileReceiver(source).receive(
                io.BytesIO(), "log.bin", timeout_s=float("nan")
            )

    def test_default_timeout_used_when_none(self):
        """timeout_s=None falls back to the 30-minute default. Smoke
        test that the default isn't accidentally 0 (which would
        immediately raise)."""
        source = _ChunkedStream([END_OF_FILE_MARKER])
        n = SdCardFileReceiver(source).receive(
            io.BytesIO(), "log.bin", timeout_s=None
        )
        assert n == 0


# ---- progress callback -----------------------------------------------------


class TestLeftoverBytes:
    """Bytes read past the EOF marker must be preserved on
    ``self.leftover`` so callers sharing the source stream don't
    lose multi-byte protocol frames that arrived in the same chunk."""

    def test_no_leftover_when_marker_at_end_of_chunk(self):
        source = _ChunkedStream([b"payload" + END_OF_FILE_MARKER])
        receiver = SdCardFileReceiver(source)
        receiver.receive(io.BytesIO(), "log.bin", timeout_s=2.0)
        assert receiver.leftover == b""

    def test_leftover_captures_post_marker_chunk_bytes(self):
        # Marker arrives mid-chunk; the bytes after the marker
        # represent something the caller cares about (e.g. the start
        # of a streaming-status frame on a shared transport).
        post_marker = b"next-frame-data"
        source = _ChunkedStream([
            b"payload" + END_OF_FILE_MARKER + post_marker,
        ])
        receiver = SdCardFileReceiver(source)
        n = receiver.receive(io.BytesIO(), "log.bin", timeout_s=2.0)
        assert n == len(b"payload")
        assert receiver.leftover == post_marker

    def test_leftover_when_marker_spans_boundary_with_trailing_data(self):
        # Marker starts at end of chunk 1, finishes in chunk 2 followed
        # by extra data.
        post_marker = b"after-marker"
        chunks = [
            b"head" + END_OF_FILE_MARKER[:5],
            END_OF_FILE_MARKER[5:] + post_marker,
        ]
        source = _ChunkedStream(chunks)
        receiver = SdCardFileReceiver(source)
        receiver.receive(io.BytesIO(), "log.bin", timeout_s=2.0)
        assert receiver.leftover == post_marker

    def test_leftover_resets_on_subsequent_receive(self):
        # Each receive() must reset leftover so a re-used receiver
        # doesn't expose stale bytes. Swap source via a new receiver
        # instance rather than mutating ``_source`` (private).
        first_receiver = SdCardFileReceiver(
            _ChunkedStream([b"a" + END_OF_FILE_MARKER + b"first-tail"])
        )
        first_receiver.receive(io.BytesIO(), "log.bin", timeout_s=2.0)
        assert first_receiver.leftover == b"first-tail"

        second_receiver = SdCardFileReceiver(
            _ChunkedStream([b"b" + END_OF_FILE_MARKER])
        )
        second_receiver.receive(io.BytesIO(), "log.bin", timeout_s=2.0)
        assert second_receiver.leftover == b""

    def test_leftover_initially_empty(self):
        source = _ChunkedStream([END_OF_FILE_MARKER])
        receiver = SdCardFileReceiver(source)
        assert receiver.leftover == b""


class TestProgressCallback:
    def test_progress_fires_with_bytes_received_and_filename(self):
        events: list[SdCardTransferProgress] = []
        source = _ChunkedStream([b"chunk-one", b"chunk-two", END_OF_FILE_MARKER])
        dest = io.BytesIO()

        SdCardFileReceiver(source).receive(
            dest, "test-log.bin", progress=events.append, timeout_s=2.0
        )

        assert events  # at least one event fired
        # Final event should reflect total bytes written.
        assert events[-1].bytes_received == len(b"chunk-onechunk-two")
        assert events[-1].file_name == "test-log.bin"

    def test_progress_callback_failure_doesnt_abort_receive(self):
        def bad_progress(_p: SdCardTransferProgress) -> None:
            raise RuntimeError("intentional")

        source = _ChunkedStream([b"payload", END_OF_FILE_MARKER])
        dest = io.BytesIO()

        n = SdCardFileReceiver(source).receive(
            dest, "log.bin", progress=bad_progress, timeout_s=2.0
        )
        assert n == len(b"payload")
        assert dest.getvalue() == b"payload"
