"""#191: an unplugged serial device is reported lost.

pyserial's port stays "open" after a USB unplug; every read then raises.
The device used to stay Connected while its reader logged ~10 errors a
second forever. The reader now reports each read's outcome to the
transport, and IO_FAULT_LIMIT failures in a row (no success between) are
taken as the device gone, as upstream daqifi-core does (#403/#382).
"""

import time

import pytest
import serial

from daqifi import transport as tmod
from daqifi._internal.scpi import ConnectionStatus
from daqifi.devices import DaqifiDevice


class _FakePort:
    """serial.Serial stand-in: idle reads until unplugged, then every read
    raises as pyserial does on a vanished device."""

    def __init__(self, **kw):
        self.is_open = True
        self.timeout = kw.get("timeout")
        self.unplugged = False
        self.flaky = 0
        self.dtr = self.rts = None

    def read(self, n=1):
        if self.unplugged:
            raise serial.SerialException(
                "device reports readiness to read but returned no data (device disconnected?)"
            )
        if self.flaky:
            self.flaky -= 1
            raise serial.SerialException("transient")
        time.sleep(0.02)
        return b""

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return 0

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False


@pytest.fixture
def connected(monkeypatch):
    ports = []

    def _serial(**kw):
        # Windows opens a COM port exclusively: a second open while a
        # handle is still open is refused.
        if any(p.is_open for p in ports):
            raise serial.SerialException("Access is denied (port already open)")
        port = _FakePort(**kw)
        ports.append(port)
        return port

    monkeypatch.setattr(tmod.serial, "Serial", _serial)
    transport = tmod.SerialStreamTransport("FAKE")
    statuses = []
    transport.add_status_changed(lambda a: statuses.append((a.is_connected, a.error)))
    device = DaqifiDevice("fake", transport=transport, message_factory=lambda b: bytes(b))
    device.connect()
    yield device, transport, ports[-1], statuses
    try:
        device.disconnect()
    except Exception:
        pass


def _wait(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_an_unplugged_device_is_reported_lost(connected) -> None:
    device, transport, port, statuses = connected
    assert device.status == ConnectionStatus.Connected
    port.unplugged = True
    assert _wait(lambda: device.status == ConnectionStatus.Lost)
    assert not transport.is_connected
    lost = [s for s in statuses[1:] if not s[0]]
    assert len(lost) == 1 and isinstance(lost[0][1], serial.SerialException)
    assert _wait(lambda: not device._consumer.is_running)


def test_a_transient_read_failure_does_not_disconnect(connected) -> None:
    device, transport, port, statuses = connected
    port.flaky = getattr(tmod.SerialStreamTransport, "IO_FAULT_LIMIT", 5) - 1
    time.sleep(1.0)
    assert device.status == ConnectionStatus.Connected
    assert transport.is_connected
    assert all(s[0] for s in statuses[1:])


def test_our_own_disconnect_is_not_a_loss(connected) -> None:
    device, transport, port, statuses = connected
    device.disconnect()
    assert device.status == ConnectionStatus.Disconnected
    assert [s for s in statuses[1:] if not s[0] and s[1] is not None] == []


def test_a_reconnect_rearms_loss_detection(connected) -> None:
    device, transport, port, statuses = connected
    port.unplugged = True
    assert _wait(lambda: device.status == ConnectionStatus.Lost)
    # Straight back to connect(), no disconnect() first (audit r1 (3)): the
    # lost handle must be closed before the port is opened again.
    device.connect()
    assert device.status == ConnectionStatus.Connected and transport.is_connected
    new_port = transport._serial
    new_port.unplugged = True
    assert _wait(lambda: device.status == ConnectionStatus.Lost)


# Audit r1 on #214.

def test_a_loss_event_from_an_earlier_connection_is_ignored(connected) -> None:
    device, transport, port, statuses = connected
    old_session = transport.session
    device.disconnect()
    device.connect()
    assert transport.session != old_session
    transport._notify_status(False, serial.SerialException("late"), session=old_session)
    time.sleep(0.2)
    assert device.status == ConnectionStatus.Connected
    assert device._consumer is not None and device._consumer.is_running


def test_read_faults_from_an_earlier_connection_do_not_count(connected) -> None:
    device, transport, port, statuses = connected
    old_session = transport.session
    device.disconnect()
    device.connect()
    for _ in range(transport.IO_FAULT_LIMIT + 2):
        transport.report_io_fault(serial.SerialException("stale"), session=old_session)
    time.sleep(0.2)
    assert device.status == ConnectionStatus.Connected and transport.is_connected


def test_a_loss_while_connecting_is_not_overwritten_by_connected(monkeypatch) -> None:
    from daqifi import communication

    class _Lossy(tmod.SerialStreamTransport):
        IO_FAULT_LIMIT = 1

    ports = []

    def _serial(**kw):
        port = _FakePort(**kw)
        port.unplugged = True  # opens, but every read fails
        ports.append(port)
        return port

    monkeypatch.setattr(tmod.serial, "Serial", _serial)
    real_start = communication.StreamMessageConsumer.start
    transport = _Lossy("FAKE")

    def _start(self):
        real_start(self)
        # Hold connect() until the reader's first fault has been reported,
        # so the loss lands while the device is still connecting.
        _wait(lambda: transport._lost, timeout=2.0)

    monkeypatch.setattr(communication.StreamMessageConsumer, "start", _start)
    device = DaqifiDevice("fake", transport=transport, message_factory=lambda b: bytes(b))
    try:
        device.connect()
        assert device.status == ConnectionStatus.Lost
    finally:
        try:
            device.disconnect()
        except Exception:
            pass


def test_a_text_exchange_reading_an_old_stream_cannot_fail_the_new_connection(connected) -> None:
    # Audit r2: the exchange captured the old stream, paused, a reconnect
    # opened session 2 meanwhile, and the text reader's sink was then bound
    # to session 2, so the closed old port's faults failed the new one.
    device, transport, port, statuses = connected
    old_port = port
    consumer = device._consumer
    real_stop = consumer.stop_safely

    def _stop_and_reconnect():
        result = real_stop()
        # Another thread's reconnect, reduced to what this exchange can see:
        # the transport and the device have moved on to a newer session.
        with transport._io_lock:
            transport._session += 1
        device._transport_session = transport.session
        old_port.unplugged = True  # the stream this exchange holds has gone
        return result

    consumer.stop_safely = _stop_and_reconnect
    device.execute_text_command(lambda: None, response_timeout_ms=1000, completion_timeout_ms=200)
    time.sleep(0.2)
    assert transport.is_connected
