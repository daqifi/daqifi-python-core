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
    device.disconnect()
    device.connect()
    assert device.status == ConnectionStatus.Connected and transport.is_connected
    new_port = transport._serial
    new_port.unplugged = True
    assert _wait(lambda: device.status == ConnectionStatus.Lost)
