"""Tests for the StreamMessageConsumer port (daqifi/communication.py @
upstream e8d84af). Covers lifecycle, parser callback wiring, error
routing, and clear_buffer behavior. Uses pure-Python BytesIO and a
custom controllable stream — no real device needed."""

from __future__ import annotations

import io
import threading
import time

import pytest

from daqifi.communication import (
    LineBasedMessageParser,
    StreamMessageConsumer,
)


pytestmark = pytest.mark.portomatic_generated


class _ControllableStream:
    """File-like that delivers chunks via a queue, simulating a slow
    serial port. ``feed(data)`` queues bytes for the next ``read`` call;
    ``close_write()`` makes future reads return empty (EOF-like)."""

    def __init__(self):
        self._chunks = []
        self._lock = threading.Lock()
        self._closed = False
        self.in_waiting = 0  # mimic pyserial.Serial

    def feed(self, data: bytes):
        with self._lock:
            self._chunks.append(bytes(data))
            self.in_waiting = sum(len(c) for c in self._chunks)

    def close_write(self):
        with self._lock:
            self._closed = True

    def read(self, n: int) -> bytes:
        # Short poll: mimic pyserial's `timeout=0.1`. Returning quickly
        # on empty lets stop_safely cancel the consumer thread inside
        # one read tick.
        deadline = time.time() + 0.1
        while time.time() < deadline:
            with self._lock:
                if self._chunks:
                    chunk = self._chunks.pop(0)
                    self.in_waiting = sum(len(c) for c in self._chunks)
                    return chunk[:n]
                if self._closed:
                    return b""
            time.sleep(0.01)
        return b""


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestStreamMessageConsumerLifecycle:
    def test_initial_state(self):
        c = StreamMessageConsumer(io.BytesIO(b""), LineBasedMessageParser())
        assert not c.is_running
        assert c.queued_message_count == 0

    def test_start_then_stop_safely(self):
        stream = _ControllableStream()
        c = StreamMessageConsumer(stream, LineBasedMessageParser())
        c.start()
        assert c.is_running
        ok = c.stop_safely(timeout_ms=500)
        assert ok is True
        assert not c.is_running

    def test_double_start_is_noop(self):
        stream = _ControllableStream()
        c = StreamMessageConsumer(stream, LineBasedMessageParser())
        c.start()
        # Second start should not spawn a second thread.
        c.start()
        c.stop()
        assert not c.is_running

    def test_close_makes_subsequent_calls_raise(self):
        c = StreamMessageConsumer(io.BytesIO(b""), LineBasedMessageParser())
        c.close()
        with pytest.raises(RuntimeError, match="closed"):
            c.start()


class TestStreamMessageConsumerEvents:
    def test_message_received_fires_for_each_complete_line(self):
        stream = _ControllableStream()
        c = StreamMessageConsumer(stream, LineBasedMessageParser())
        received = []

        def on_msg(args):
            received.append(args.message.data)

        c.add_message_received(on_msg)
        c.start()
        try:
            stream.feed(b"*IDN?\r\nREAD?\r\n")
            assert _wait_for(lambda: len(received) >= 2)
            assert received == ["*IDN?", "READ?"]
        finally:
            c.stop_safely()

    def test_partial_line_stays_buffered(self):
        stream = _ControllableStream()
        c = StreamMessageConsumer(stream, LineBasedMessageParser())
        received = []
        c.add_message_received(lambda a: received.append(a.message.data))
        c.start()
        try:
            stream.feed(b"PARTIAL")
            time.sleep(0.1)
            assert received == []
            stream.feed(b"-MORE\r\n")
            assert _wait_for(lambda: received == ["PARTIAL-MORE"])
        finally:
            c.stop_safely()

    def test_remove_handler_stops_callbacks(self):
        stream = _ControllableStream()
        c = StreamMessageConsumer(stream, LineBasedMessageParser())
        received = []
        h = lambda a: received.append(a.message.data)
        c.add_message_received(h)
        c.remove_message_received(h)
        # Removing again is a no-op.
        c.remove_message_received(h)
        c.start()
        try:
            stream.feed(b"X\r\n")
            time.sleep(0.1)
            assert received == []
        finally:
            c.stop_safely()


class TestStreamMessageConsumerErrors:
    def test_parser_exception_routes_to_error_handler(self):
        class _BadParser:
            def parse_messages(self, data):
                raise RuntimeError("boom")

        stream = _ControllableStream()
        c = StreamMessageConsumer(stream, _BadParser())
        errors = []
        c.add_error_occurred(lambda a: errors.append(a.error))
        c.start()
        try:
            stream.feed(b"any")
            assert _wait_for(lambda: len(errors) >= 1)
            assert isinstance(errors[0], RuntimeError)
        finally:
            c.stop_safely()

    def test_stream_read_exception_routes_to_error_handler(self):
        class _BoomStream:
            def read(self, n):
                raise OSError("disconnected")

        c = StreamMessageConsumer(_BoomStream(), LineBasedMessageParser())
        errors = []
        c.add_error_occurred(lambda a: errors.append(a.error))
        c.start()
        try:
            assert _wait_for(lambda: len(errors) >= 1)
            assert isinstance(errors[0], OSError)
        finally:
            c.stop_safely()


class TestStreamMessageConsumerBuffer:
    def test_clear_buffer_drains_internal_state(self):
        stream = _ControllableStream()
        c = StreamMessageConsumer(stream, LineBasedMessageParser())
        c.start()
        try:
            stream.feed(b"PARTIAL")  # incomplete -- stays in buffer
            assert _wait_for(lambda: c.queued_message_count > 0)
            c.clear_buffer()
            assert c.queued_message_count == 0
        finally:
            c.stop_safely()

    def test_constructor_validation(self):
        with pytest.raises(TypeError):
            StreamMessageConsumer(None, LineBasedMessageParser())  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            StreamMessageConsumer(io.BytesIO(b""), None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            StreamMessageConsumer(io.BytesIO(b""), LineBasedMessageParser(), buffer_size=0)
