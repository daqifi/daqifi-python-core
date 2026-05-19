"""Tests for ticket #148 — wire StreamManager._read_sample_protobuf
through the real ProtobufMessageParser instead of returning a
hardcoded ``Sample(channels={0: 0.0})`` placeholder.

The legacy implementation read bytes, printed a hex dump, and
returned the same fake sample for every read regardless of payload.
These tests verify the new path returns actual decoded values, maps
positions correctly when streaming a subset of channels, and handles
batched frames + partial reads.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.exceptions import CommandError
from daqifi.streaming import Sample, StreamManager, StreamState


def _length_prefix(length: int) -> bytes:
    """Encode a single integer as a protobuf varint length prefix."""
    out = bytearray()
    while length > 0x7F:
        out.append((length & 0x7F) | 0x80)
        length >>= 7
    out.append(length & 0x7F)
    return bytes(out)


def _wire_frame(message: DaqifiOutMessage) -> bytes:
    payload = message.SerializeToString()
    return _length_prefix(len(payload)) + payload


def _make_comm_with_bytes(buf: bytes) -> MagicMock:
    """Build a SerialInterface stub whose `_serial.in_waiting` and
    `_serial.read(n)` drain ``buf`` in one or more chunks."""
    serial = MagicMock()
    state = {"remaining": buf}

    def _in_waiting():
        return len(state["remaining"])

    def _read(n):
        chunk = state["remaining"][:n]
        state["remaining"] = state["remaining"][n:]
        return chunk

    # `in_waiting` is a property on real pyserial Serial. The
    # StreamManager probes it with `self._comm._serial.in_waiting`,
    # which on a MagicMock returns another MagicMock unless we wire
    # `__int__`. Easier: set the side_effect on a callable.
    type(serial).in_waiting = property(lambda self: _in_waiting())
    serial.read = MagicMock(side_effect=_read)
    comm = MagicMock()
    comm._serial = serial
    return comm


class TestReadSampleProtobufDecodesRealValues:
    def test_single_channel_single_sample(self):
        """One frame, one channel, one float value → one Sample with
        that exact value (not the {0: 0.0} placeholder)."""
        msg = DaqifiOutMessage()
        msg.msg_time_stamp = 12345
        msg.analog_in_data_float.append(2.769)
        comm = _make_comm_with_bytes(_wire_frame(msg))

        sm = StreamManager(comm, channels=[0], format=0)
        sample = sm._read_sample_protobuf()

        assert sample is not None
        # ``analog_in_data_float`` is protobuf float32 — assert with
        # tolerance, not exact equality.
        assert sample.channels[0] == pytest.approx(2.769)

    def test_multi_channel_position_mapping(self):
        """Three enabled channels — analog_in_data_float[i] maps to
        the i-th channel in the StreamManager's channel list, not to
        channel index i."""
        msg = DaqifiOutMessage()
        msg.msg_time_stamp = 1000
        for v in (1.5, 2.5, 3.5):
            msg.analog_in_data_float.append(v)
        comm = _make_comm_with_bytes(_wire_frame(msg))

        sm = StreamManager(comm, channels=[0, 2, 5], format=0)
        sample = sm._read_sample_protobuf()

        assert sample is not None
        assert sample.channels[0] == pytest.approx(1.5)
        assert sample.channels[2] == pytest.approx(2.5)
        assert sample.channels[5] == pytest.approx(3.5)
        assert set(sample.channels.keys()) == {0, 2, 5}

    def test_falls_back_to_raw_analog_in_data_when_float_missing(self):
        """WiFi firmware sends raw ADC counts in ``analog_in_data``
        when ``analog_in_data_float`` is empty. The decoder uses the
        raw value as a float (unscaled — caller knows to use
        DaqifiStreamingDevice for calibrated values)."""
        msg = DaqifiOutMessage()
        msg.msg_time_stamp = 50
        msg.analog_in_data.append(1843)
        comm = _make_comm_with_bytes(_wire_frame(msg))

        sm = StreamManager(comm, channels=[0], format=0)
        sample = sm._read_sample_protobuf()

        assert sample is not None
        assert sample.channels == {0: 1843.0}

    def test_batched_frame_produces_multiple_samples(self):
        """A frame carrying 4 values for 2 channels yields 2 Samples
        with the per-block channel-to-value mapping intact. The
        second is queued and returned on the next call."""
        msg = DaqifiOutMessage()
        msg.msg_time_stamp = 100
        for v in (1.0, 2.0, 3.0, 4.0):  # 2 samples × 2 channels
            msg.analog_in_data_float.append(v)
        for t in (10, 20):
            msg.analog_in_data_ts.append(t)
        comm = _make_comm_with_bytes(_wire_frame(msg))

        sm = StreamManager(comm, channels=[0, 1], format=0)
        sm._tick_rate_hz = 1000  # so ts conversion is deterministic

        first = sm._read_sample_protobuf()
        second = sm._read_sample_protobuf()

        assert first is not None and second is not None
        assert first.channels[0] == pytest.approx(1.0)
        assert first.channels[1] == pytest.approx(2.0)
        assert second.channels[0] == pytest.approx(3.0)
        assert second.channels[1] == pytest.approx(4.0)
        assert first.timestamp == 10 / 1000.0
        assert second.timestamp == 20 / 1000.0

    def test_returns_none_when_no_data(self):
        """No bytes buffered → None, and no parser side effects."""
        comm = _make_comm_with_bytes(b"")
        sm = StreamManager(comm, channels=[0], format=0)
        assert sm._read_sample_protobuf() is None

    def test_partial_frame_buffers_and_completes_on_next_call(self):
        """Splitting a single frame across two reads still produces
        exactly one decoded Sample once the tail bytes arrive."""
        msg = DaqifiOutMessage()
        msg.msg_time_stamp = 7
        msg.analog_in_data_float.append(0.125)
        frame = _wire_frame(msg)
        split = len(frame) // 2

        # First read returns only the prefix half — parser shouldn't
        # produce a sample yet.
        comm = _make_comm_with_bytes(frame[:split])
        sm = StreamManager(comm, channels=[0], format=0)
        assert sm._read_sample_protobuf() is None

        # Re-stock the stub with the tail half, then re-read.
        # Replacing the property is awkward; easier to drive the
        # StreamManager's internal buffer directly with the rest.
        sm._pb_buffer += frame[split:]
        sample = sm._read_sample_protobuf()
        assert sample is not None
        assert sample.channels[0] == pytest.approx(0.125)

    def test_buffer_hard_cap_drops_old_garbage_on_desync(self):
        """When the parser repeatedly returns (no messages, 0 bytes
        consumed) — the real-world signal of a stuck wait-for-tail
        state — the buffer is trimmed to its hard cap to prevent
        unbounded growth. Verifies the safety cap added in pass-1
        review (Qodo /improve #1 importance 7)."""
        comm = _make_comm_with_bytes(b"")
        sm = StreamManager(comm, channels=[0], format=0)
        sm._PB_BUFFER_HARD_CAP = 32  # tiny cap so the test stays fast
        sm._pb_buffer.extend(b"\xab" * 100)  # well over the cap

        # Force the parser into "made no progress" by stubbing it.
        # Real desync scenarios that produce this state: an apparent
        # partial frame whose declared length never arrives because
        # the device disconnected mid-write.
        sm._pb_parser = MagicMock()
        sm._pb_parser.parse_messages.return_value = ([], 0)

        sm._read_sample_protobuf()

        # Cap trims to half the cap (16 bytes); content is the tail
        # of the original buffer, not the head.
        assert len(sm._pb_buffer) == 16
        assert all(b == 0xAB for b in sm._pb_buffer)

    def test_misaligned_frame_drops_silently(self):
        """If the per-frame value count isn't a clean multiple of the
        channel list, decoder returns no sample for that frame
        (mirrors the JSON/CSV paths that drop malformed lines
        silently rather than crashing the streaming thread)."""
        msg = DaqifiOutMessage()
        msg.msg_time_stamp = 100
        # 3 floats but 2 enabled channels — caller has no defensible
        # way to assign value[2] to a channel.
        for v in (1.0, 2.0, 3.0):
            msg.analog_in_data_float.append(v)
        comm = _make_comm_with_bytes(_wire_frame(msg))

        sm = StreamManager(comm, channels=[0, 1], format=0)
        assert sm._read_sample_protobuf() is None


class TestProtobufStateLifecycle:
    """Cover the lifecycle of the new PB decode state across start/
    stop cycles (Qodo PR #149 /agentic_review #2)."""

    def test_start_clears_leftover_pb_buffer_and_pending_samples(self):
        """A stop()'d StreamManager that gets start()ed again must
        not surface stale buffered bytes or queued samples from the
        previous session. Without the reset, the very first
        _read_sample_protobuf call in the new session would pop a
        sample from the OLD session's queue."""
        comm = _make_comm_with_bytes(b"")
        sm = StreamManager(comm, channels=[0], format=0)

        # Simulate a previous session leaving residue behind.
        sm._pb_buffer.extend(b"\xde\xad\xbe\xef")
        sm._pending_pb_samples.append(
            Sample(timestamp=0.0, channels={0: 99.9})
        )

        # start() does its real-side-effect machinery; mock the
        # SCPI sends so we don't actually drive a device.
        sm._comm.send_set = MagicMock()
        sm._comm.send_command = MagicMock(return_value="OK")

        # Don't pass a callback, so start() runs synchronously and
        # transitions to RUNNING without spawning a thread.
        sm.start()

        assert len(sm._pb_buffer) == 0
        assert len(sm._pending_pb_samples) == 0


class TestProtobufExceptionWrapping:
    """The synchronous read() path must surface PB transport /
    parser errors as CommandError, not raw pyserial or protobuf
    exceptions (Qodo PR #149 /agentic_review #3)."""

    def test_serial_read_exception_wrapped(self):
        serial = MagicMock()
        # `in_waiting` succeeds, but the .read() that follows raises
        # — represents a pyserial transport error mid-read.
        type(serial).in_waiting = property(lambda self: 8)
        serial.read = MagicMock(side_effect=OSError("port closed"))
        comm = MagicMock()
        comm._serial = serial

        sm = StreamManager(comm, channels=[0], format=0)
        with pytest.raises(CommandError, match="Failed to read PB stream"):
            sm._read_sample_protobuf()

    def test_in_waiting_exception_wrapped(self):
        serial = MagicMock()

        def _boom(self):
            raise OSError("device disconnected")

        type(serial).in_waiting = property(_boom)
        comm = MagicMock()
        comm._serial = serial

        sm = StreamManager(comm, channels=[0], format=0)
        with pytest.raises(CommandError, match="Failed to read PB stream"):
            sm._read_sample_protobuf()

    def test_parser_exception_wrapped(self):
        comm = _make_comm_with_bytes(b"\x01\x02")
        sm = StreamManager(comm, channels=[0], format=0)
        sm._pb_parser = MagicMock()
        sm._pb_parser.parse_messages.side_effect = RuntimeError("bad varint")

        with pytest.raises(CommandError, match="Failed to read PB stream"):
            sm._read_sample_protobuf()


class TestStreamManagerReadProtobufEndToEnd:
    """Higher-level: walk a real PB frame through StreamManager.read
    (the blocking read path) instead of calling
    _read_sample_protobuf directly. Catches wiring breaks between
    the new decoder and the public API (Qodo PR #149
    /agentic_review #1)."""

    def test_read_returns_decoded_sample_via_blocking_api(self):
        msg = DaqifiOutMessage()
        msg.msg_time_stamp = 200
        msg.analog_in_data_float.append(1.5)
        msg.analog_in_data_float.append(2.5)
        comm = _make_comm_with_bytes(_wire_frame(msg))

        sm = StreamManager(comm, channels=[0, 1], format=0)
        sm._state = StreamState.RUNNING  # bypass start()'s SCPI side effects

        samples = sm.read(num_samples=1, timeout=2.0)

        assert len(samples) == 1
        assert samples[0].channels[0] == pytest.approx(1.5)
        assert samples[0].channels[1] == pytest.approx(2.5)
