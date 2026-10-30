"""#200 audit r1: SD file operations over the PRODUCTION TCP transport.

The SD readers polled ``getattr(raw_stream, "in_waiting", 0)``, and the TCP
transport's ``_SocketStream`` had no ``in_waiting``, so over WiFi nothing was
ever read: a download timed out with 0 bytes, a listing came back
incomplete, and a delete raised after the device may already have deleted
the file. The serial fake used before supplied ``in_waiting`` and hid it.

These tests run a real DaqifiStreamingDevice on a real TcpStreamTransport
against a fake device on a loopback socket.
"""

import io
import socket
import threading

import pytest

from daqifi.devices import DaqifiStreamingDevice
from daqifi.transport import TcpStreamTransport, _SocketStream


class _FakeDevice:
    """A loopback 'device' answering SD commands as the firmware does."""

    def __init__(self, listing=b"Daqifi/log_a.bin 3\r\n", file_bytes=b"abc", silent_after=None):
        self.listing = listing
        self.file_bytes = file_bytes
        # A command after which the device answers nothing more (a link
        # that drops just after the device acted on it).
        self.silent_after = silent_after
        self.silent = False
        self.commands = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        buf = b""
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    command = line.strip().decode(errors="replace")
                    self.commands.append(command)
                    if self.silent_after and command.startswith(self.silent_after):
                        self.silent = True
                    if self.silent:
                        continue
                    if command.startswith("SYSTem:ERRor?"):
                        conn.sendall(b'0,"No error"\r\n')
                    elif command.startswith("SYSTem:STORage:SD:LIST?"):
                        conn.sendall(self.listing)
                    elif command.startswith("SYSTem:STORage:SD:GET"):
                        conn.sendall(self.file_bytes + b"__END_OF_FILE__\r\n")

    def close(self):
        self._server.close()


@pytest.fixture
def device_on(request):
    made = []

    def _make(fake):
        device = DaqifiStreamingDevice(name="tcp-test", transport=TcpStreamTransport("127.0.0.1", fake.port))
        device.connect()
        device._metadata.firmware_version = "3.7.0"
        made.append((device, fake))
        return device

    yield _make
    for device, fake in made:
        device.disconnect()
        fake.close()


def test_listing_over_tcp_reads_the_reply(device_on) -> None:
    fake = _FakeDevice()
    device = device_on(fake)
    files = device.get_sd_card_files()
    assert [f.file_name for f in files] == ["log_a.bin"]
    assert "SYSTem:COMMunicate:LAN:ENAbled 0" not in fake.commands


def test_download_over_tcp_reads_the_file(device_on) -> None:
    fake = _FakeDevice(file_bytes=b"hello-sd")
    device = device_on(fake)
    out = io.BytesIO()
    result = device.download_sd_card_file("log_a.bin", out, expected_size=8)
    assert out.getvalue() == b"hello-sd" and result.size_verified


def test_delete_over_tcp_is_confirmed(device_on) -> None:
    fake = _FakeDevice(listing=b"")
    device = device_on(fake)
    device.delete_sd_card_file("log_a.bin")
    assert any(c.startswith("SYSTem:STORage:SD:DELete") for c in fake.commands)


def test_an_unconfirmed_delete_says_the_outcome_is_unknown(device_on) -> None:
    from daqifi.exceptions import SdCardDeleteOutcomeUnknownException

    # The device answers the drain before the delete, then nothing once the
    # DELete arrives: it may have deleted the file.
    fake = _FakeDevice(silent_after="SYSTem:STORage:SD:DELete")
    device = device_on(fake)
    with pytest.raises(SdCardDeleteOutcomeUnknownException, match="may already be gone"):
        device.delete_sd_card_file("log_a.bin")
    assert any(c.startswith("SYSTem:STORage:SD:DELete") for c in fake.commands)


def test_the_tcp_stream_reports_bytes_waiting_without_consuming_them() -> None:
    a, b = socket.socketpair()
    try:
        closed = []
        stream = _SocketStream(a, 0.5, 1, lambda s: closed.append(s))
        assert stream.in_waiting == 0
        b.sendall(b"12345")
        deadline = threading.Event()
        for _ in range(50):
            if stream.in_waiting:
                break
            deadline.wait(0.01)
        assert stream.in_waiting == 5
        assert stream.read(5) == b"12345"
        b.close()
        assert stream.in_waiting == 0 and stream.peer_closed and closed
    finally:
        a.close()
