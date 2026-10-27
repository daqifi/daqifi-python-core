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
