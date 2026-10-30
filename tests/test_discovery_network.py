"""#196: discovery and network.

1. Neither serial finder filtered by VID/PID, so both wrote SCPI to every
   port that existed (DeviceFinder: COM1..COM255 on Windows; SerialDeviceFinder
   including SYSTem:POWer:STATe 1). Only DAQiFi application ports (04D8:F794,
   plus CDC-ACM ports whose IDs can't be read) are probed now (upstream
   daqifi-core#201).
2. One hung port blocked SerialDeviceFinder.discover() forever. Each probe is
   now bounded by a hard timeout, and a port whose abandoned probe is still
   running isn't probed again (upstream daqifi-core#295).
3. On macOS DeviceFinder listed every board twice (tty.* and cu.*).
4. WiFiDeviceFinder never filled local_interface_address, and TCP never bound
   to it, so a multi-homed host connected over the default route (upstream
   daqifi-core#187).
5. update_network_configuration sent LAN:SAVE after LAN:APPLY; over a TCP
   control link the APPLY drops the link and the SAVE never arrived. It now
   ends with LAN:APPLY 1, which applies and, only if the apply is accepted,
   saves in one firmware handler (upstream daqifi-core#391 saves first, which
   would persist a configuration the apply then rejects).
"""

import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from serial.tools import list_ports

from daqifi.device_finders import (
    ConnectionType,
    DeviceInfo,
    DeviceType,
    SerialDeviceFinder,
    WiFiDeviceFinder,
)
from daqifi.devices import DaqifiDeviceFactory
from daqifi.discovery import DeviceFinder
from daqifi.enums import WifiMode, WifiSecurityType
from daqifi.network import NetworkConfiguration
from daqifi.transport import TcpStreamTransport

try:  # absent before #196; the tests that need it then fail as tests
    import daqifi._internal.usb_ports as usb_ports
except ImportError:  # pragma: no cover
    usb_ports = None


def _port(device, vid=None, pid=None):
    return SimpleNamespace(device=device, vid=vid, pid=pid)


_LISTING = [
    _port("COM41", 0x04D8, 0xF794),          # a DAQiFi board
    _port("COM42", 0x2341, 0x0043),          # an Arduino
    _port("/dev/ttyACM5"),                   # CDC-ACM, IDs unreadable: may be a board
    _port("/dev/ttyS0"),                     # built-in UART: can't be a board
    _port("COM43", 0x04D8, 0x003C),          # a DAQiFi bootloader (HID): not a serial target
]


@pytest.fixture
def listing(monkeypatch):
    monkeypatch.setattr(list_ports, "comports", lambda: list(_LISTING))


@pytest.fixture(autouse=True)
def _no_stuck_probes():
    yield
    if usb_ports is not None:
        usb_ports._live.clear()


# ---- 1: VID/PID filter --------------------------------------------------


def test_only_daqifi_application_ports_are_listed(listing) -> None:
    assert usb_ports.daqifi_serial_ports() == ["/dev/ttyACM5", "COM41"]
    assert DeviceFinder.get_serial_ports() == ["/dev/ttyACM5", "COM41"]


def test_device_finder_probes_only_daqifi_ports(listing, monkeypatch) -> None:
    probed = []
    monkeypatch.setattr(DeviceFinder, "_identify_device", staticmethod(lambda p, cancelled=None: probed.append(p)))
    DeviceFinder.discover()
    assert sorted(probed) == ["/dev/ttyACM5", "COM41"]


def test_serial_device_finder_probes_only_daqifi_ports(listing, monkeypatch) -> None:
    probed = []
    monkeypatch.setattr(SerialDeviceFinder, "_try_get_device_info", lambda self, p, cancelled=None: probed.append(p))
    SerialDeviceFinder().discover()
    assert sorted(probed) == ["/dev/ttyACM5", "COM41"]


def test_an_explicit_port_filter_is_probed_as_given(listing, monkeypatch) -> None:
    probed = []
    monkeypatch.setattr(SerialDeviceFinder, "_try_get_device_info", lambda self, p, cancelled=None: probed.append(p))
    SerialDeviceFinder().discover(port_filter=["COM42"])
    assert probed == ["COM42"]


def test_a_failed_listing_probes_nothing(monkeypatch) -> None:
    def _broken():
        raise OSError("no sysfs")

    monkeypatch.setattr(list_ports, "comports", _broken)
    assert DeviceFinder.get_serial_ports() == []


# ---- 2: a hung port -----------------------------------------------------


def _info(port):
    return DeviceInfo(name=port, serial_number="", firmware_version="",
                      connection_type=ConnectionType.SERIAL, device_type=DeviceType.UNKNOWN,
                      port_name=port)


def test_a_hung_port_doesnt_block_discovery_and_isnt_probed_again(monkeypatch) -> None:
    import daqifi.device_finders as device_finders

    monkeypatch.setattr(device_finders, "_SERIAL_PROBE_HARD_TIMEOUT_S", 0.2)
    monkeypatch.setattr(list_ports, "comports",
                        lambda: [_port("COM50", 0x04D8, 0xF794), _port("COM51", 0x04D8, 0xF794)])
    release = threading.Event()
    calls = []

    def probe(self, port, cancelled=None):
        calls.append(port)
        if port == "COM50":
            release.wait(10)  # a driver that never returns
            return None
        return _info(port)

    monkeypatch.setattr(SerialDeviceFinder, "_try_get_device_info", probe)
    finder = SerialDeviceFinder()
    try:
        started = time.monotonic()
        found = finder.discover()
        assert time.monotonic() - started < 2.0
        assert [d.port_name for d in found] == ["COM51"]
        finder.discover()
        assert calls.count("COM50") == 1  # the stuck probe isn't doubled
    finally:
        release.set()


def test_a_port_is_probed_again_once_its_stuck_probe_ends() -> None:
    release = threading.Event()
    calls = []

    def probe(port, cancelled):
        calls.append(port)
        if len(calls) == 1:
            release.wait(10)
        return port

    assert usb_ports.run_port_probe("COM58", probe, 0.1) is None
    stuck = next(iter(usb_ports._live["COM58"]))
    release.set()
    stuck.join(2)
    assert usb_ports.run_port_probe("COM58", probe, 1.0) == "COM58"


# ---- round 2 ------------------------------------------------------------


def test_each_hard_timeout_exceeds_its_probes_worst_healthy_run() -> None:
    import daqifi.device_finders as device_finders
    import daqifi.discovery as discovery

    assert device_finders._SERIAL_PROBE_HARD_TIMEOUT_S > device_finders._SERIAL_PROBE_WORST_HEALTHY_S
    assert discovery._IDENTIFY_HARD_TIMEOUT_S > discovery._IDENTIFY_WORST_HEALTHY_S
    # The SerialDeviceFinder probe can legitimately take: wake-up 1 s; four
    # init writes (each up to the 1 s write timeout) and 0.1 s waits; the 4 s
    # response loop and its up-to-3 GetDeviceInfo writes (1 s each); a
    # straddling 1 s read; a 0.1 s poll.
    assert device_finders._SERIAL_PROBE_WORST_HEALTHY_S >= 1 + 4 * 1.1 + 4 + 3 + 1 + 0.1


def test_a_board_that_answers_on_its_retry_is_still_found(monkeypatch) -> None:
    # A healthy probe that runs ~3.5 s (the device answers its second
    # GetDeviceInfo) must not be abandoned.
    monkeypatch.setattr(list_ports, "comports", lambda: [_port("COM52", 0x04D8, 0xF794)])

    def slow_probe(self, port, cancelled=None):
        time.sleep(3.5)
        return _info(port)

    monkeypatch.setattr(SerialDeviceFinder, "_try_get_device_info", slow_probe)
    assert [d.port_name for d in SerialDeviceFinder().discover()] == ["COM52"]


def test_concurrent_probes_of_one_port_never_run_twice() -> None:
    # Two discoveries racing on one port: the second sees the first's live
    # probe and skips, even before the first times out; after the first is
    # abandoned it stays tracked, so a third doesn't start either.
    release = threading.Event()
    started = threading.Event()
    calls = []

    def probe(port, cancelled):
        calls.append(port)
        started.set()
        release.wait(10)
        return port

    first = threading.Thread(target=usb_ports.run_port_probe, args=("COM53", probe, 0.3))
    first.start()
    started.wait(2)
    assert usb_ports.run_port_probe("COM53", probe, 0.3) is None   # concurrent
    first.join(2)
    assert usb_ports.run_port_probe("COM53", probe, 0.3) is None   # after abandonment
    assert calls == ["COM53"]
    release.set()


# ---- 3: macOS duplicates ------------------------------------------------


def test_a_board_is_listed_once_on_macos(monkeypatch) -> None:
    monkeypatch.setattr(list_ports, "comports", lambda: [
        _port("/dev/tty.usbmodem1101", 0x04D8, 0xF794),
        _port("/dev/cu.usbmodem1101", 0x04D8, 0xF794),
    ])
    assert DeviceFinder.get_serial_ports() == ["/dev/cu.usbmodem1101"]


# ---- 4: the discovering NIC ---------------------------------------------


class _OneReply:
    def __init__(self):
        self.sent = False

    def send_broadcast_to(self, data, addr):
        pass

    def receive(self, timeout):
        if self.sent:
            time.sleep(0.01)
            return None
        self.sent = True
        return b"DaqifiNyq1\r\n", ("192.168.7.40", 30303)


def test_wifi_discovery_records_the_nic_a_device_answered_on() -> None:
    finder = WiFiDeviceFinder()
    found = []
    finder._collect_on_transport(_OneReply(), time.monotonic() + 0.2, set(), found,
                                 threading.Lock(), broadcast_addr="192.168.7.255",
                                 local_interface_address="192.168.7.2")
    assert [d.local_interface_address for d in found] == ["192.168.7.2"]


class _RecordingSocket:
    calls = []

    def __init__(self, *args):
        pass

    def settimeout(self, t):
        pass

    def bind(self, addr):
        _RecordingSocket.calls.append(("bind", addr))

    def connect(self, addr):
        _RecordingSocket.calls.append(("connect", addr))

    def close(self):
        pass

    def fileno(self):
        return -1


def test_tcp_binds_to_the_local_interface_before_connecting(monkeypatch) -> None:
    import daqifi.transport as transport_module

    _RecordingSocket.calls = []
    monkeypatch.setattr(transport_module.socket, "socket", _RecordingSocket)
    transport = TcpStreamTransport("192.168.7.40", 9760, local_interface="192.168.7.2")
    assert transport.local_interface == "192.168.7.2"
    transport._open_connection(SimpleNamespace(connection_timeout=1.0))
    assert _RecordingSocket.calls[:2] == [("bind", ("192.168.7.2", 0)),
                                          ("connect", ("192.168.7.40", 9760))]


def test_tcp_without_a_local_interface_doesnt_bind(monkeypatch) -> None:
    import daqifi.transport as transport_module

    _RecordingSocket.calls = []
    monkeypatch.setattr(transport_module.socket, "socket", _RecordingSocket)
    TcpStreamTransport("192.168.7.40", 9760)._open_connection(SimpleNamespace(connection_timeout=1.0))
    assert [c[0] for c in _RecordingSocket.calls][:1] == ["connect"]


def test_connecting_from_device_info_leaves_by_the_discovering_nic(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(DaqifiDeviceFactory, "_connect_with_transport",
                        staticmethod(lambda transport, *a, **k: seen.append(transport)))
    info = DeviceInfo(name="d", serial_number="", firmware_version="",
                      connection_type=ConnectionType.WIFI, device_type=DeviceType.UNKNOWN,
                      ip_address="192.168.7.40", port=9760, local_interface_address="192.168.7.2")
    DaqifiDeviceFactory.connect_from_device_info(info)
    assert seen[0].local_interface == "192.168.7.2"


# ---- 5: APPLY 1 -----------------------------------------------------------

_spec = importlib.util.spec_from_file_location(
    "net_fakes", Path(__file__).parent / "portomatic-generated" / "test_streaming_device_network_config.py"
)
_net = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_net)


def test_the_configuration_is_applied_and_saved_in_one_command_sent_last() -> None:
    sd = _net._make_device()
    try:
        sd.update_network_configuration(NetworkConfiguration(
            ssid="NewAP", mode=WifiMode.EXISTING_NETWORK,
            security_type=WifiSecurityType.WPA_PSK_PHRASE, password="pw"))
        commands = [w.decode().strip() for w in sd._transport.stream.written]
        lan = [c for c in commands if "LAN:" in c or "SD:ENAble" in c]
        assert lan[-3:] == [
            "SYSTem:STORage:SD:ENAble 0",
            "SYSTem:COMMunicate:LAN:ENAbled 1",
            "SYSTem:COMMunicate:LAN:APPLY 1",
        ]
        assert commands[-1] == "SYSTem:COMMunicate:LAN:APPLY 1"
        assert "SYSTem:COMMunicate:LAN:SAVE" not in commands
    finally:
        sd.close()


# ---- round 3 ------------------------------------------------------------


class _BlockedOpenSerial:
    """A serial port whose open blocks until released: the open completes
    only after discovery has given up on the probe. Records every action."""

    release = None
    instances = []

    def __init__(self, *args, **kwargs):
        _BlockedOpenSerial.instances.append(self)
        self.writes = []
        self.dtr_set = False
        self.closed = False
        self.is_open = True
        self.in_waiting = 0
        _BlockedOpenSerial.release.wait(10)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def dtr(self):
        return self.dtr_set

    @dtr.setter
    def dtr(self, value):
        self.dtr_set = True

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def read(self, n=1):
        return b""

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def close(self):
        self.closed = True
        self.is_open = False


@pytest.fixture
def blocked_open(monkeypatch):
    import serial

    _BlockedOpenSerial.release = threading.Event()
    _BlockedOpenSerial.instances = []
    monkeypatch.setattr(serial, "Serial", _BlockedOpenSerial)
    yield _BlockedOpenSerial
    _BlockedOpenSerial.release.set()


def _wait_for_probe_threads(port, timeout=15.0):
    deadline = time.monotonic() + timeout
    while usb_ports._live.get(port) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not usb_ports._live.get(port), "probe thread didn't end"


def test_an_abandoned_serial_probe_sends_nothing_once_its_open_completes(monkeypatch, blocked_open) -> None:
    # The open blocks past the hard timeout, then completes: the abandoned
    # probe must not wake the board or send STOP / a format change, which
    # would break a newer session on it.
    import daqifi.device_finders as device_finders

    monkeypatch.setattr(device_finders, "_SERIAL_PROBE_HARD_TIMEOUT_S", 0.2)
    monkeypatch.setattr(list_ports, "comports", lambda: [_port("COM54", 0x04D8, 0xF794)])
    assert SerialDeviceFinder().discover() == []
    blocked_open.release.set()
    _wait_for_probe_threads("COM54")
    (port,) = blocked_open.instances
    assert port.writes == [] and not port.dtr_set
    assert port.closed


def test_an_abandoned_idn_probe_sends_nothing_once_its_open_completes(monkeypatch, blocked_open) -> None:
    import daqifi.discovery as discovery

    monkeypatch.setattr(discovery, "_IDENTIFY_HARD_TIMEOUT_S", 0.2)
    monkeypatch.setattr(list_ports, "comports", lambda: [_port("COM55", 0x04D8, 0xF794)])
    assert DeviceFinder.discover() == []
    blocked_open.release.set()
    _wait_for_probe_threads("COM55")
    (port,) = blocked_open.instances
    assert port.writes == []
    assert port.closed
