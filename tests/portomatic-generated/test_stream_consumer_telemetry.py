"""StreamMessageConsumer.telemetry counters (closes #18 §3).

The consumer thread reads bytes from a stream, hands the buffer to a
parser, and emits parsed messages. The telemetry counters surface
read syscall behavior + parse cost so an operator can answer:
"is the bottleneck the device, the OS buffer, the parser, or the
downstream callbacks?" — without attaching a profiler.
"""
from __future__ import annotations

import io
import threading
import time
from typing import List, Tuple

import pytest

from daqifi.communication import (
    InboundMessage,
    StreamConsumerTelemetry,
    StreamMessageConsumer,
)


pytestmark = pytest.mark.portomatic_generated


# ---------------------------------------------------------------------------
# StreamConsumerTelemetry — dataclass behavior
# ---------------------------------------------------------------------------

def test_telemetry_zero_init():
    t = StreamConsumerTelemetry()
    assert t.read_calls == 0
    assert t.read_bytes_total == 0
    assert t.read_bytes_max == 0
    assert t.read_seconds == 0.0
    assert t.parse_calls == 0
    assert t.parse_seconds == 0.0
    assert t.messages_emitted == 0


def test_telemetry_avg_handles_zero_calls():
    """No reads yet → avg is 0.0, not a ZeroDivisionError."""
    assert StreamConsumerTelemetry().read_bytes_avg == 0.0


def test_telemetry_avg_after_reads():
    t = StreamConsumerTelemetry(read_calls=4, read_bytes_total=400)
    assert t.read_bytes_avg == 100.0


# ---------------------------------------------------------------------------
# Live consumer — counters advance as the consumer thread runs
# ---------------------------------------------------------------------------

class _OneShotStream:
    """Yields a fixed payload exactly once, then returns empty bytes
    on subsequent reads (simulating idle stream)."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._yielded = False
        self._lock = threading.Lock()

    def read(self, n: int) -> bytes:
        with self._lock:
            if self._yielded:
                # Block briefly so the consumer thread doesn't burn CPU.
                time.sleep(0.005)
                return b""
            self._yielded = True
            return self._payload[:n]


class _CountingParser:
    """Parses every byte as one message — easy to predict counts."""

    def __init__(self):
        self.calls = 0

    def parse_messages(self, data: bytes) -> Tuple[List[InboundMessage[bytes]], int]:
        self.calls += 1
        msgs = [InboundMessage(data=bytes([b])) for b in data]
        return msgs, len(data)


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_consumer_telemetry_reflects_reads_parses_messages():
    payload = b"abcdef"
    stream = _OneShotStream(payload)
    parser = _CountingParser()
    consumer = StreamMessageConsumer(stream=stream, message_parser=parser)
    consumer.start()
    try:
        # Wait for the stream's one payload to be drained + parsed.
        assert _wait_until(
            lambda: consumer.telemetry.messages_emitted == len(payload)
        ), f"expected {len(payload)} msgs, got {consumer.telemetry.messages_emitted}"
    finally:
        consumer.stop()
        consumer.close()

    tel = consumer.telemetry
    assert tel.read_calls >= 1, "at least one productive read must be counted"
    assert tel.read_bytes_total >= len(payload)
    assert tel.read_bytes_max >= len(payload)
    assert tel.read_seconds > 0
    assert tel.parse_calls >= 1
    assert tel.parse_seconds >= 0  # may round to 0 on a fast machine
    assert tel.messages_emitted == len(payload)


def test_consumer_start_resets_telemetry():
    """Each start/stop cycle reports its own counters."""
    stream = _OneShotStream(b"xy")
    parser = _CountingParser()
    consumer = StreamMessageConsumer(stream=stream, message_parser=parser)
    consumer.start()
    try:
        assert _wait_until(lambda: consumer.telemetry.messages_emitted == 2)
    finally:
        consumer.stop()

    first_cycle = consumer.telemetry
    assert first_cycle.messages_emitted == 2

    # Re-arm with a fresh payload and start again.
    stream2 = _OneShotStream(b"abc")
    consumer._stream = stream2  # private surgery: the test is the API customer
    consumer.start()
    try:
        assert _wait_until(lambda: consumer.telemetry.messages_emitted == 3)
    finally:
        consumer.stop()
        consumer.close()

    # Second cycle's telemetry is a fresh instance; first_cycle still
    # reflects the prior run (caller can hold onto it post-stop).
    assert consumer.telemetry is not first_cycle
    assert consumer.telemetry.messages_emitted == 3
    assert first_cycle.messages_emitted == 2  # unchanged


def test_start_refuses_when_prior_thread_alive():
    """If stop() times out (1 s join) and the prior thread is still
    alive, start() must NOT spawn a second thread (would race two
    daemon readers on the same stream and corrupt telemetry —
    Qodo PR #68 review #3)."""

    class _SlowStream:
        """Read blocks for 5 s — longer than stop()'s 1 s join timeout."""

        def read(self, n: int) -> bytes:
            time.sleep(5.0)
            return b""

    consumer = StreamMessageConsumer(
        stream=_SlowStream(), message_parser=_CountingParser(),
    )
    consumer.start()
    try:
        # Issue a stop — it should time out internally because read() blocks.
        consumer.stop()
        # Thread is still alive (the read() is mid-flight).
        assert consumer._thread is not None
        assert consumer._thread.is_alive()

        # start() must refuse rather than spawn a second thread.
        with pytest.raises(RuntimeError, match="previous consumer thread"):
            consumer.start()
    finally:
        # Daemon thread will be reaped at interpreter exit — no clean
        # shutdown path for a permanently blocked read().
        pass


def test_parse_telemetry_counts_failed_invocations():
    """Even when parse_messages raises, the call + duration are counted
    so an operator sees the parser is broken (Qodo PR #68 review #5)."""

    class _AlwaysRaisingParser:
        def __init__(self):
            self.calls = 0

        def parse_messages(self, data: bytes):
            self.calls += 1
            raise RuntimeError("parser is intentionally broken")

    stream = _OneShotStream(b"x")
    parser = _AlwaysRaisingParser()
    consumer = StreamMessageConsumer(stream=stream, message_parser=parser)
    consumer.start()
    try:
        # Wait for at least one parse attempt.
        assert _wait_until(lambda: parser.calls >= 1)
        # Give the consumer time to record the failure.
        time.sleep(0.05)
    finally:
        consumer.stop()
        consumer.close()

    tel = consumer.telemetry
    assert tel.parse_calls >= 1, (
        f"failed parse_messages must still bump parse_calls; got {tel.parse_calls}"
    )


def test_consumer_idle_stream_does_not_inflate_read_calls():
    """Empty reads (timeout / no data) should not bump read_calls —
    only productive reads count, so the operator can spot a stream
    that's silent."""

    class _AlwaysEmpty:
        def read(self, n: int) -> bytes:
            time.sleep(0.005)
            return b""

    consumer = StreamMessageConsumer(
        stream=_AlwaysEmpty(), message_parser=_CountingParser(),
    )
    consumer.start()
    try:
        time.sleep(0.1)
    finally:
        consumer.stop()
        consumer.close()

    assert consumer.telemetry.read_calls == 0
    assert consumer.telemetry.read_bytes_total == 0
    assert consumer.telemetry.messages_emitted == 0
    # idle_reads is the gap-detection primitive — should rise each
    # spin where stream.read() returned nothing.
    assert consumer.telemetry.idle_reads > 0
