"""TCP transport: bounded reads and peer-close detection (#164 items 1-2).

Each test runs a real DaqifiDevice + TcpStreamTransport against a localhost
socket the test owns; no hardware.
"""
from __future__ import annotations

import socket
import threading
import time

from daqifi._internal.comm import ScpiMessage
from daqifi._internal.scpi import ConnectionStatus
from daqifi.devices import DaqifiDevice
from daqifi.transport import TcpStreamTransport


class _FakeDevice:
    """Accepts one connection and answers SYST:ERR? with "No error"."""

    def __init__(self) -> None:
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self.conn = None
        self._accepted = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        self.conn, _ = self._server.accept()
        self._accepted.set()
        buf = b""
        while True:
            try:
                data = self.conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                if line == b"SYST:ERR?":
                    self.conn.sendall(b'0,"No error"\r\n')

    def wait_accepted(self) -> None:
        assert self._accepted.wait(5)

    def send_frame(self) -> None:
        self.conn.sendall(bytes([3, 0x08, 0x01, 0x10]))

    def close(self) -> None:
        try:
            if self.conn is not None:
                self.conn.close()
        finally:
            self._server.close()


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _connect(fake: _FakeDevice):
    transport = TcpStreamTransport("127.0.0.1", fake.port)
    device = DaqifiDevice("fake", transport=transport, message_factory=lambda b: bytes(b))
    device.connect()
    fake.wait_accepted()
    return transport, device


def test_text_exchanges_over_tcp_keep_the_protobuf_consumer_alive():
    # Audit finding (#164 item 1): the protobuf reader blocked forever in
    # a socket read, so pausing it for a text exchange timed out, its
    # restart failed ("previous thread still alive", swallowed) and no
    # frame was dispatched again until reconnect.
    before = set(threading.enumerate())
    fake = _FakeDevice()
    transport, device = _connect(fake)
    try:
        received = []
        device._consumer.add_message_received(received.append)
        for _ in range(3):
            started = time.monotonic()
            reply = device.execute_text_command(
                lambda: device.send(ScpiMessage("SYST:ERR?")), 1000, 200
            )
            assert reply == ['0,"No error"']
            assert time.monotonic() - started < 2.5  # was ~4.2 s per exchange
            assert device._consumer.is_running
        # Only this test's readers: other tests may leave their own behind.
        readers = [
            t for t in threading.enumerate()
            if t not in before and t.name.startswith("MessageConsumer")
        ]
        assert len(readers) == 1
        for _ in range(3):
            fake.send_frame()
            time.sleep(0.05)
        assert _wait_until(lambda: len(received) == 3)
    finally:
        device.disconnect()
        fake.close()


def test_device_closing_the_connection_is_detected_as_lost():
    # Audit finding (#164 item 2): EOF from the device read as an endless
    # run of idle reads; the session stayed "connected" with no event.
    fake = _FakeDevice()
    transport, device = _connect(fake)
    transport_events = []
    transport.add_status_changed(transport_events.append)
    try:
        # shutdown sends the FIN even while the fake's own reader thread is
        # still blocked in recv on this socket (close alone would not).
        fake.conn.shutdown(socket.SHUT_RDWR)
        assert _wait_until(lambda: device.status == ConnectionStatus.Lost)
        assert not transport.is_connected
        assert any(
            not e.is_connected and isinstance(e.error, ConnectionResetError)
            for e in transport_events
        )
        assert device._consumer is None  # torn down, not spinning
    finally:
        device.disconnect()
        fake.close()


def test_own_disconnect_is_not_reported_as_the_device_going_away():
    fake = _FakeDevice()
    transport, device = _connect(fake)
    transport_events = []
    transport.add_status_changed(transport_events.append)
    try:
        device.disconnect()
        time.sleep(0.3)
        assert [(e.is_connected, e.error) for e in transport_events] == [(False, None)]
        assert device.status == ConnectionStatus.Disconnected
    finally:
        fake.close()


def test_late_loss_event_does_not_tear_down_a_reconnected_session():
    # Audit finding on #167 (round 1): a handler registered before the
    # device blocked on the loss event; the device reconnected meanwhile,
    # and the stale event then stopped the NEW consumer.
    fake = _FakeDevice()
    transport = TcpStreamTransport("127.0.0.1", fake.port)
    release = threading.Event()
    blocked = threading.Event()

    def _slow_handler(args):
        if args.error is not None and not args.is_connected:
            blocked.set()
            release.wait(5)

    transport.add_status_changed(_slow_handler)
    device = DaqifiDevice("fake", transport=transport, message_factory=lambda b: bytes(b))
    device.connect()
    fake.wait_accepted()
    second = None
    try:
        first_conn = fake.conn
        fake._accepted.clear()
        threading.Thread(target=fake._serve, daemon=True).start()
        first_conn.shutdown(socket.SHUT_RDWR)
        assert blocked.wait(3)
        device.disconnect()
        device.connect()
        fake.wait_accepted()
        second = fake.conn
        release.set()
        time.sleep(0.5)
        assert device.status == ConnectionStatus.Connected
        assert device._consumer is not None and device._consumer.is_running
        received = []
        device._consumer.add_message_received(received.append)
        fake.send_frame()
        assert _wait_until(lambda: len(received) == 1)
    finally:
        release.set()
        device.disconnect()
        fake.close()


def test_reconnect_during_a_loss_teardown_keeps_the_new_session():
    # Audit finding on #167 (round 2): the loss handler passed its session
    # check, then sat in the old consumer's stop while the app reconnected;
    # resuming, it closed the NEW consumer.
    fake = _FakeDevice()
    transport, device = _connect(fake)
    release = threading.Event()
    in_teardown = threading.Event()
    old_consumer = device._consumer
    real_stop = old_consumer.stop_safely

    def _slow_stop(*a, **k):
        # Stall only the loss handler, which runs on the old consumer's own
        # reader thread; the app's own disconnect must not be held up.
        if threading.current_thread() is old_consumer._thread:
            in_teardown.set()
            release.wait(5)
        return real_stop(*a, **k)

    old_consumer.stop_safely = _slow_stop
    try:
        first_conn = fake.conn
        fake._accepted.clear()
        threading.Thread(target=fake._serve, daemon=True).start()
        first_conn.shutdown(socket.SHUT_RDWR)
        assert in_teardown.wait(3)

        def _reconnect():
            device.disconnect()
            device.connect()

        reconnect = threading.Thread(target=_reconnect)
        reconnect.start()
        time.sleep(0.3)
        release.set()
        reconnect.join(10)
        assert not reconnect.is_alive()
        fake.wait_accepted()
        assert device.status == ConnectionStatus.Connected
        assert device._consumer is not None and device._consumer is not old_consumer
        assert device._consumer.is_running
        received = []
        device._consumer.add_message_received(received.append)
        fake.send_frame()
        assert _wait_until(lambda: len(received) == 1)
    finally:
        release.set()
        device.disconnect()
        fake.close()


def test_peer_closing_during_connect_is_not_overwritten_by_connected(monkeypatch):
    # Audit finding on #167 (round 2): the loss handler ran while connect()
    # was still finishing, and connect() then set Connected over Lost.
    import daqifi.devices as devices

    real_start = devices.StreamMessageConsumer.start

    def _start_then_pause(self, *a, **k):
        result = real_start(self, *a, **k)
        time.sleep(0.5)  # the peer's FIN is read and reported meanwhile
        return result

    monkeypatch.setattr(devices.StreamMessageConsumer, "start", _start_then_pause)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def _accept_and_close():
        conn, _ = server.accept()
        conn.shutdown(socket.SHUT_RDWR)
        conn.close()

    threading.Thread(target=_accept_and_close, daemon=True).start()
    transport = TcpStreamTransport("127.0.0.1", server.getsockname()[1])
    device = DaqifiDevice("fake", transport=transport, message_factory=lambda b: bytes(b))
    events = []
    device.add_status_changed(lambda args: events.append(args.status))
    try:
        device.connect()
        assert _wait_until(lambda: device.status == ConnectionStatus.Lost)
        assert not transport.is_connected
        # Subscribers must not be left believing it is connected.
        assert events[-1] == ConnectionStatus.Lost
        assert ConnectionStatus.Connected not in events[events.index(ConnectionStatus.Lost):]
    finally:
        device.disconnect()
        server.close()


def test_status_handler_can_wait_for_a_reconnect_on_another_thread():
    # Audit finding on #167 (round 3): status handlers ran under the
    # lifecycle lock, so a handler that hands a reconnect to a worker and
    # waits for it deadlocked (the worker needs the lock).
    fake = _FakeDevice()
    transport, device = _connect(fake)
    finished = threading.Event()
    joined_in_time = []

    def _on_status(args):
        if args.status == ConnectionStatus.Lost:
            def _reconnect():
                device.disconnect()
                device.connect()
                finished.set()

            worker = threading.Thread(target=_reconnect, daemon=True)
            worker.start()
            worker.join(3)
            # With the deadlock, the worker can't finish until this handler
            # gives up waiting and returns.
            joined_in_time.append(not worker.is_alive())

    device.add_status_changed(_on_status)
    try:
        first_conn = fake.conn
        fake._accepted.clear()
        threading.Thread(target=fake._serve, daemon=True).start()
        first_conn.shutdown(socket.SHUT_RDWR)
        assert finished.wait(8)
        assert _wait_until(lambda: joined_in_time)  # the handler records after its join
        assert joined_in_time == [True]
        fake.wait_accepted()
        assert device.status == ConnectionStatus.Connected
    finally:
        device.disconnect()
        fake.close()


def test_a_stale_eof_report_does_not_mute_the_next_connection():
    # Audit findings on #167 (rounds 1 and 3): a loss reported late for an
    # old connection must neither tear down nor mute the new one.
    fake = _FakeDevice()
    transport, device = _connect(fake)
    try:
        old_stream = transport.stream
        fake._accepted.clear()
        threading.Thread(target=fake._serve, daemon=True).start()
        device.disconnect()
        device.connect()
        fake.wait_accepted()
        # The old reader reporting late, as if it slipped past every guard.
        old_stream.loss_reported = False
        transport._on_peer_closed(old_stream)
        time.sleep(0.2)
        assert device.status == ConnectionStatus.Connected
        assert device._consumer is not None and device._consumer.is_running
        fake.conn.shutdown(socket.SHUT_RDWR)
        assert _wait_until(lambda: device.status == ConnectionStatus.Lost)
    finally:
        device.disconnect()
        fake.close()


def test_failed_connect_still_notifies_its_status_changes():
    # Audit finding on #167 (round 4): status events queued during a
    # connect that raised were discarded, so subscribers saw nothing.
    import pytest

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()  # nothing listens here: the connect is refused
    device = DaqifiDevice("fake", transport=TcpStreamTransport("127.0.0.1", port))
    events = []
    device.add_status_changed(lambda args: events.append(args.status))
    with pytest.raises(Exception):
        device.connect()
    assert events and events[0] == ConnectionStatus.Connecting
    assert events[-1] == device.status


def test_transport_handler_can_wait_on_a_worker_that_reconnects():
    # Audit finding on #167 (round 4): transport callbacks ran under the
    # device's lifecycle lock, so one waiting on a worker that calls
    # device.connect() deadlocked.
    fake = _FakeDevice()
    transport, device = _connect(fake)
    joined_in_time = []

    def _on_transport(args):
        if not args.is_connected and not joined_in_time:
            worker = threading.Thread(target=lambda: device.connect(), daemon=True)
            fake._accepted.clear()
            threading.Thread(target=fake._serve, daemon=True).start()
            worker.start()
            worker.join(3)
            joined_in_time.append(not worker.is_alive())

    transport.add_status_changed(_on_transport)
    try:
        device.disconnect()
        assert _wait_until(lambda: joined_in_time)
        assert joined_in_time == [True]
    finally:
        transport.remove_status_changed(_on_transport)
        device.disconnect()
        fake.close()


def test_sd_download_over_tcp_tolerates_gaps_longer_than_a_read_window():
    # Audit finding on #167 (round 4): the 0.5 s read bound raised
    # socket.timeout out of SdCardFileReceiver, failing a transfer whose
    # data paused for longer, well within its own deadline.
    import io as _io

    from daqifi.sdcard import SdCardFileReceiver

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def _send_late():
        conn, _ = server.accept()
        time.sleep(0.8)
        conn.sendall(b"abc__END_OF_FILE__")
        time.sleep(0.5)
        conn.close()

    threading.Thread(target=_send_late, daemon=True).start()
    transport = TcpStreamTransport("127.0.0.1", server.getsockname()[1])
    transport.connect()
    try:
        destination = _io.BytesIO()
        written = SdCardFileReceiver(transport.stream).receive(destination, "log.bin", timeout_s=10)
        assert written == 3 and destination.getvalue() == b"abc"
    finally:
        transport.disconnect()
        server.close()
