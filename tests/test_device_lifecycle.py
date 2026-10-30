"""#201: device lifecycle drift from daqifi-core.

1. initialize() on serial reported Ready with empty metadata (one
   SYSInfoPB?, a 1 s poll, no error), and sent its setup commands
   fire-and-forget, so a refused one passed silently. It now re-sends
   SYSInfoPB? every second until a status reports channels (else raises,
   upstream 8c97c47), and checks the setup commands with a SYSTem:ERRor?
   terminator, retrying once (upstream 1ce13b2 / 1142893).
2. send() racing disconnect() raised a raw ValueError and overwrote
   Disconnected with Lost/Error (upstream baabf0c).
3. execute_text_command had no overall cap (upstream caps every exchange
   at 5x the first-response timeout), and a bare-LF or blank-only reply
   waited out the timeout (upstream a57a86a / 5a9f917).
4. Typed errors: serial open failures are named (upstream da74a4c),
   not-connected guards raise DeviceNotConnectedError (d76b2f5), and
   ConnectionRetryOptions rejects invalid values (14fb907).
"""

import errno
import threading
import time

import pytest
import serial

import daqifi.exceptions as exc_mod
from daqifi._internal.comm import ConnectionRetryOptions
from daqifi._internal.scpi import ConnectionStatus, DeviceState
from daqifi.devices import DaqifiStreamingDevice


def _status_frame() -> bytes:
    from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage

    body = DaqifiOutMessage(analog_in_port_num=2, device_sn=1234).SerializeToString()
    return bytes([len(body)]) + body


class _Stream:
    """A scripted device with an error queue. ``stale_errors`` are in the
    queue from the start; ``refuse`` maps a command prefix to how many times
    the device refuses it (None: always), each refusal queueing -200;
    SYSTem:ERRor? pops the queue (``0,"No error"`` when empty).
    ``status_after`` is how many SYSInfoPB? requests go unanswered before
    one is answered (None: never); ``on_write`` maps a command prefix to
    bytes to send back; ``frame_before_replies`` is sent ahead of every
    SYSTem:ERRor? reply (a stream still running)."""

    def __init__(self, stale_errors=0, refuse=None, status_after=0, on_write=None,
                 frame_before_replies=b"", status_delay=0.0, reply_delay=0.0):
        self.written = []
        self.queue = [b'-200,"Execution error"'] * stale_errors
        self.refuse = dict(refuse or {})
        self.status_after = status_after
        self.info_requests = 0
        self.on_write = dict(on_write or {})
        self.frame_before_replies = frame_before_replies
        self.status_delay = status_delay
        self.reply_delay = reply_delay
        self._rx = bytearray()
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._rx.extend(data)

    def write(self, data) -> int:
        b = bytes(data)
        self.written.append(b)
        for prefix, times in self.refuse.items():
            if b.startswith(prefix) and (times is None or times > 0):
                self.queue.append(b'-200,"Execution error"')
                if times is not None:
                    self.refuse[prefix] = times - 1
        if b.startswith(b"SYSTem:ERRor?"):
            reply = self.queue.pop(0) if self.queue else b'0,"No error"'
            data = self.frame_before_replies + reply + b"\r\n"
            if self.reply_delay:
                timer = threading.Timer(self.reply_delay, self.feed, (data,))
                timer.daemon = True
                timer.start()
            else:
                self.feed(data)
        elif b.startswith(b"SYSTem:SYSInfoPB?"):
            self.info_requests += 1
            if self.status_after is not None and self.info_requests > self.status_after:
                if self.status_delay:
                    timer = threading.Timer(self.status_delay, self.feed, (_status_frame(),))
                    timer.daemon = True
                    timer.start()
                else:
                    self.feed(_status_frame())
        for prefix, reply in self.on_write.items():
            if b.startswith(prefix):
                if isinstance(reply, tuple):  # (bytes, delay_s)
                    timer = threading.Timer(reply[1], self.feed, (reply[0],))
                    timer.daemon = True
                    timer.start()
                else:
                    self.feed(reply)
        return len(b)

    def flush(self) -> None:
        pass

    def read(self, n: int) -> bytes:
        with self._lock:
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    def commands(self):
        return [w.decode(errors="replace").strip() for w in self.written]


def _device(stream):
    from daqifi.transport import SerialStreamTransport

    class _Transport(SerialStreamTransport):
        def __init__(self):
            self._stream_obj = stream
            self._connected = False
            self.handlers = []

        @property
        def stream(self):
            return self._stream_obj

        @property
        def is_connected(self):
            return self._connected

        def add_status_changed(self, h):
            self.handlers.append(h)

        def remove_status_changed(self, h):
            if h in self.handlers:
                self.handlers.remove(h)

        def connect(self, retry_options=None):
            self._connected = True

        def disconnect(self):
            self._connected = False

        def close(self):
            self._connected = False

    device = DaqifiStreamingDevice(name="d", transport=_Transport())
    device.connect()
    return device


# ---- 1: initialize ------------------------------------------------------------


def test_serial_initialize_without_a_status_raises_instead_of_reporting_ready() -> None:
    device = _device(_Stream(status_after=None))
    try:
        with pytest.raises(TimeoutError, match="channel configuration"):
            device.initialize(response_timeout_s=0.5)
        assert device.state != DeviceState.Ready
    finally:
        device.disconnect()


def test_a_slow_status_reply_is_waited_for_with_one_query_and_none_left_owed() -> None:
    # SYSInfoPB? answered 2.5 s late. One query, waited for: Ready with the
    # channels, and nothing still owed to land in the next text reply (a
    # re-send every second left replies due at 3.5 and 4.5 s).
    from daqifi._internal.scpi_commands import ScpiMessage

    stream = _Stream(status_delay=2.5, on_write={b"X?": (b"VALUE\r\n", 1.2)})
    device = _device(stream)
    try:
        device.initialize(response_timeout_s=6.0)
        assert device.state == DeviceState.Ready
        assert len(device.channels) == 2
        assert stream.info_requests == 1
        lines = device.execute_text_command(
            lambda: device.send(ScpiMessage("X?")),
            response_timeout_ms=2500, completion_timeout_ms=1500,
        )
        assert lines == ["VALUE"]
    finally:
        device.disconnect()


def test_a_persistently_refused_init_command_raises_typed() -> None:
    stream = _Stream(refuse={b"SYSTem:POWer:STATe 1": None})
    device = _device(stream)
    try:
        with pytest.raises(exc_mod.CommandError, match="-200") as info:
            device.initialize(response_timeout_s=1.0)
        assert type(info.value).__name__ == "ScpiInitializationError"
        assert device.state == DeviceState.Error
    finally:
        device.disconnect()


def test_a_refusal_the_device_also_volunteers_raises() -> None:
    # The firmware's SCPI_ErrorPush both queues the error and writes
    # "**ERROR: ..." to the wire (SCPI_USB_Error / SCPI_TCP_Error).
    stream = _Stream(
        refuse={b"SYSTem:POWer:STATe 1": None},
        on_write={b"SYSTem:POWer:STATe 1": b"**ERROR: -200, \"Execution error\"\r\n"},
    )
    device = _device(stream)
    try:
        with pytest.raises(exc_mod.CommandError, match="-200"):
            device.initialize(response_timeout_s=1.0)
    finally:
        device.disconnect()


def test_a_transient_init_error_is_retried_once() -> None:
    stream = _Stream(refuse={b"SYSTem:POWer:STATe 1": 1})
    device = _device(stream)
    try:
        device.initialize(response_timeout_s=2.0)
        assert device.state == DeviceState.Ready
        power_on = [c for c in stream.commands() if c.startswith("SYSTem:POWer:STATe 1")]
        assert len(power_on) == 2
    finally:
        device.disconnect()


def test_errors_left_in_the_queue_dont_fail_a_healthy_init() -> None:
    # Two errors from earlier commands; every init command succeeds.
    device = _device(_Stream(stale_errors=2))
    try:
        device.initialize(response_timeout_s=2.0)
        assert device.state == DeviceState.Ready
    finally:
        device.disconnect()


def test_two_first_attempt_errors_then_a_clean_retry_initialise() -> None:
    stream = _Stream(refuse={b"SYSTem:POWer:STATe 1": 1, b"SYSTem:STReam:FORmat": 1})
    device = _device(stream)
    try:
        device.initialize(response_timeout_s=2.0)
        assert device.state == DeviceState.Ready
    finally:
        device.disconnect()


def test_a_reply_merged_with_stream_bytes_isnt_taken_for_success() -> None:
    # Still streaming (STOP refused): sample bytes with no LF land ahead of
    # every reply, so "-200,..." fuses into an unreadable line.
    stream = _Stream(
        refuse={b"SYSTem:STReam:STOP": None},
        frame_before_replies=bytes.fromhex("050801120102"),
    )
    device = _device(stream)
    try:
        with pytest.raises(exc_mod.DAQiFiError) as info:
            device.initialize(response_timeout_s=1.0)
        assert type(info.value).__name__ == "InitializationUnconfirmedError"
        # Unknown, not refused: no Ready, and no Error on a guess.
        assert device.state not in (DeviceState.Ready, DeviceState.Error)
    finally:
        device.disconnect()


class _BlockingReadStream(_Stream):
    """pyserial's read semantics: ``read(n)`` returns early only once n
    bytes are in; otherwise at the read timeout (0.5 s, as the transport
    sets after open), here plus a 30 ms scheduling delay."""

    timeout = 0.5

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._arrived = threading.Condition(self._lock)

    def feed(self, data: bytes) -> None:
        with self._arrived:
            self._rx.extend(data)
            self._arrived.notify_all()

    def read(self, n: int) -> bytes:
        deadline = time.monotonic() + self.timeout + 0.03
        with self._arrived:
            while len(self._rx) < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._arrived.wait(remaining)
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out


def test_init_reads_short_replies_on_a_port_that_blocks_for_its_read_timeout() -> None:
    # A healthy device answering at once; the text reader must not wait out
    # the port's read timeout for each short reply.
    device = _device(_BlockingReadStream())
    try:
        device.initialize(response_timeout_s=4.0)
        assert device.state == DeviceState.Ready
    finally:
        device.disconnect()


def test_no_status_reply_is_left_owed_to_corrupt_the_next_text_reply() -> None:
    # Each SYSInfoPB? is answered 1.1 s later (a re-send at 1 s would cross
    # it). A text query straight after initialize(), answered 1.2 s later,
    # must read clean: no owed frame may land in front of it.
    from daqifi._internal.scpi_commands import ScpiMessage

    device = _device(_Stream(status_delay=1.1, on_write={b"X?": (b"VALUE\r\n", 1.2)}))
    try:
        device.initialize(response_timeout_s=4.0)
        lines = device.execute_text_command(
            lambda: device.send(ScpiMessage("X?")),
            response_timeout_ms=2500, completion_timeout_ms=1500,
        )
        assert lines == ["VALUE"]
    finally:
        device.disconnect()


def test_late_error_replies_dont_confirm_a_refused_init_command() -> None:
    # Every SYSTem:ERRor? reply comes 650 ms late and power-on is always
    # refused. A query answered after its window must not leave its reply
    # to satisfy the next question: the refusal must be seen.
    stream = _Stream(refuse={b"SYSTem:POWer:STATe 1": None}, reply_delay=0.65)
    device = _device(stream)
    try:
        with pytest.raises(exc_mod.CommandError, match="-200"):
            device.initialize(response_timeout_s=3.0)
        assert device.state != DeviceState.Ready
    finally:
        device.disconnect()


def test_late_error_replies_dont_corrupt_the_status_reply() -> None:
    # Every command accepted, every SYSTem:ERRor? reply 600 ms late: no
    # reply may still be outstanding when the status query goes out.
    stream = _Stream(reply_delay=0.6)
    device = _device(stream)
    try:
        device.initialize(response_timeout_s=3.0)
        assert device.state == DeviceState.Ready
        assert len(device.channels) == 2
    finally:
        device.disconnect()


def test_error_replies_slower_than_accepted_end_unconfirmed_never_ready() -> None:
    # Replies 1.3 s late, past the 1 s init accepts: no reply can be
    # attributed, so the outcome is unknown, not Ready and not Error.
    stream = _Stream(reply_delay=1.3)
    device = _device(stream)
    try:
        with pytest.raises(exc_mod.DAQiFiError) as info:
            device.initialize(response_timeout_s=3.0)
        assert type(info.value).__name__ == "InitializationUnconfirmedError"
        assert device.state not in (DeviceState.Ready, DeviceState.Error)
    finally:
        device.disconnect()


# ---- 2: send() racing disconnect() --------------------------------------------


class _ClosingStream(_Stream):
    """A write that blocks until released, then fails as a closed stream
    does."""

    def __init__(self):
        super().__init__()
        self.block = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, data) -> int:
        if self.block.is_set():
            self.entered.set()
            self.release.wait(5)
            raise ValueError("I/O operation on closed file.")
        return super().write(data)


def test_a_send_cut_off_by_disconnect_fails_typed_and_keeps_disconnected() -> None:
    from daqifi._internal import scpi_commands

    stream = _ClosingStream()
    device = _device(stream)
    stream.block.set()
    outcome = {}

    def _send():
        try:
            device.send(scpi_commands.get_last_error())
        except BaseException as e:
            outcome["error"] = e

    sender = threading.Thread(target=_send, daemon=True)
    sender.start()
    assert stream.entered.wait(5)
    device.disconnect()  # closes the stream under the blocked write
    stream.release.set()
    sender.join(5)
    error = outcome.get("error")
    assert type(error).__name__ == "DeviceNotConnectedError", error
    assert isinstance(error, RuntimeError)
    assert error.is_shutting_down
    assert device.status == ConnectionStatus.Disconnected
    assert device.state == DeviceState.Disconnected


class _FakeSerial:
    """A ``serial.Serial`` stand-in: ``block_writes`` makes writes wait for
    ``release``, then fail as a port closed under them does."""

    def __init__(self, block_writes=False):
        self.is_open = True
        self.dtr = self.rts = False
        self.timeout = None
        self.block_writes = block_writes
        self.entered = threading.Event()
        self.release = threading.Event()
        self.written = []

    def write(self, data):
        if self.block_writes:
            self.entered.set()
            self.release.wait(5)
            raise serial.SerialException("write failed: port closed")
        self.written.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        time.sleep(0.01)
        return b""

    @property
    def in_waiting(self):
        return 0

    def close(self):
        self.is_open = False


def test_a_stale_send_failure_doesnt_mark_a_reconnected_device_lost(monkeypatch) -> None:
    # A blocks writing on the first port; B disconnects and reconnects the
    # same transport object (a new port); then A's write fails. That
    # failure belongs to the old connection, not the new one.
    import daqifi.transport as transport_mod
    from daqifi._internal import scpi_commands

    first, second = _FakeSerial(block_writes=True), _FakeSerial()
    ports = iter([first, second])
    monkeypatch.setattr(transport_mod.serial, "Serial", lambda *a, **k: next(ports))
    device = DaqifiStreamingDevice(
        name="d", transport=transport_mod.SerialStreamTransport("COM58")
    )
    device.connect()
    outcome = {}

    def _send():
        try:
            device.send(scpi_commands.get_last_error())
        except BaseException as e:
            outcome["error"] = e

    sender = threading.Thread(target=_send, daemon=True)
    try:
        sender.start()
        assert first.entered.wait(5)
        device.disconnect()
        device.connect()
        assert device.status == ConnectionStatus.Connected
        first.release.set()
        sender.join(5)
        error = outcome.get("error")
        assert type(error).__name__ == "DeviceNotConnectedError", error
        assert error.is_shutting_down
        # The new connection is untouched and still works.
        assert device.status == ConnectionStatus.Connected
        assert device.state != DeviceState.Error
        device.send(scpi_commands.get_last_error())
        assert second.written
    finally:
        first.release.set()
        device.disconnect()


def test_a_reconnect_after_the_shutdown_check_isnt_marked_lost(monkeypatch) -> None:
    # A's write fails on the live connection and its check finds no
    # shutdown; B then disconnects and reconnects. A must not mark the new
    # connection Lost: B can't get in between A's check and its transition.
    import daqifi.transport as transport_mod
    from daqifi._internal import scpi_commands

    first, second = _FakeSerial(block_writes=True), _FakeSerial()
    ports = iter([first, second])
    monkeypatch.setattr(transport_mod.serial, "Serial", lambda *a, **k: next(ports))
    device = DaqifiStreamingDevice(
        name="d", transport=transport_mod.SerialStreamTransport("COM58")
    )
    device.connect()

    checked, resume = threading.Event(), threading.Event()
    real_check = device._shutting_down_since

    def _check_then_pause(*args):
        result = real_check(*args)
        if not checked.is_set():
            checked.set()
            resume.wait(5)
        return result

    monkeypatch.setattr(device, "_shutting_down_since", _check_then_pause)
    outcome = {}

    def _send():
        try:
            device.send(scpi_commands.get_last_error())
        except BaseException as e:
            outcome["error"] = e

    def _reconnect():
        device.disconnect()
        device.connect()

    sender = threading.Thread(target=_send, daemon=True)
    other = threading.Thread(target=_reconnect, daemon=True)
    try:
        sender.start()
        assert first.entered.wait(5)
        first.release.set()  # A's write fails on the live connection
        assert checked.wait(5)
        other.start()
        other.join(1.0)  # B reconnects, if it can get in before A finishes
        resume.set()
        sender.join(5)
        other.join(5)
        assert not other.is_alive()
        assert device.status == ConnectionStatus.Connected
        assert device.state != DeviceState.Error
        device.send(scpi_commands.get_last_error())
        assert second.written
    finally:
        resume.set()
        first.release.set()
        device.disconnect()


class _PausingDevice(DaqifiStreamingDevice):
    """Pauses one chosen thread just after send()'s connected check."""

    pause = None  # (thread ident, paused Event, resume Event)

    @property
    def is_connected(self):
        result = super().is_connected
        pause = self.pause
        if pause and threading.get_ident() == pause[0] and not pause[1].is_set():
            pause[1].set()
            pause[2].wait(5)
        return result


def test_a_send_that_captured_the_old_stream_after_the_bump_spares_the_reconnect(monkeypatch) -> None:
    # Codex's three-pause schedule: A passes its connected check and
    # pauses; the port dies and B's connect() bumps the generation, then
    # pauses before the transport installs the new port; A captures (new
    # generation, old port) and its write blocks; B finishes; A's write
    # fails. The reconnected device must stay Connected.
    import daqifi.transport as transport_mod
    from daqifi._internal import scpi_commands

    first, second = _FakeSerial(block_writes=True), _FakeSerial()
    ports = iter([first, second])
    monkeypatch.setattr(transport_mod.serial, "Serial", lambda *a, **k: next(ports))
    transport = transport_mod.SerialStreamTransport("COM58")
    device = _PausingDevice(name="d", transport=transport)
    device.connect()

    a_paused, a_resume = threading.Event(), threading.Event()
    b_paused, b_resume = threading.Event(), threading.Event()
    real_connect = transport.connect

    def _pausing_connect(*args, **kwargs):
        b_paused.set()
        b_resume.wait(5)
        return real_connect(*args, **kwargs)

    outcome = {}

    def _send():
        device.pause = (threading.get_ident(), a_paused, a_resume)
        try:
            device.send(scpi_commands.get_last_error())
        except BaseException as e:
            outcome["error"] = e

    sender = threading.Thread(target=_send, daemon=True)
    reconnecter = threading.Thread(target=device.connect, daemon=True)
    try:
        sender.start()
        assert a_paused.wait(5)                # 1: A past its connected check
        first.is_open = False                  # the port dies
        monkeypatch.setattr(transport, "connect", _pausing_connect)
        reconnecter.start()
        assert b_paused.wait(5)                # 2: B bumped, new port not yet in
        a_resume.set()
        assert first.entered.wait(5)           # A captured the old port, writing
        b_resume.set()
        reconnecter.join(5)                    # 3: B finishes the reconnect
        assert device.status == ConnectionStatus.Connected
        first.release.set()                    # A's write on the old port fails
        sender.join(5)
        assert type(outcome.get("error")).__name__ == "DeviceNotConnectedError", outcome
        assert device.status == ConnectionStatus.Connected
        assert device.state != DeviceState.Error
        device.send(scpi_commands.get_last_error())
        assert second.written
    finally:
        a_resume.set()
        b_resume.set()
        first.release.set()
        device.disconnect()


class _UnpolledStream(_Stream):
    """A stream with no ``in_waiting`` whose ``read(n)`` returns early only
    once n bytes are in, else at its 0.5 s timeout."""

    timeout = 0.5

    def read(self, n: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self._rx) >= n:
                    break
            time.sleep(0.01)
        with self._lock:
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    in_waiting = property(lambda self: (_ for _ in ()).throw(AttributeError("in_waiting")))


def test_an_explicit_total_cap_is_kept_even_below_the_read_timeout() -> None:
    from daqifi._internal.scpi_commands import ScpiMessage

    stream = _UnpolledStream(on_write={b"X?": b"VALUE\r\n"})
    assert not hasattr(stream, "in_waiting")
    device = _device(stream)
    try:
        lines = device.execute_text_command(
            lambda: device.send(ScpiMessage("X?")),
            response_timeout_ms=1000, completion_timeout_ms=200, max_total_ms=50,
        )
        assert lines == []
    finally:
        device.disconnect()


def test_a_send_cut_off_by_close_is_typed_on_serial(monkeypatch) -> None:
    # close() disposes the transport, whose stream getter then raises; the
    # failed send must still report the typed error, not that.
    import daqifi.transport as transport_mod
    from daqifi._internal import scpi_commands

    port = _FakeSerial(block_writes=True)
    monkeypatch.setattr(transport_mod.serial, "Serial", lambda *a, **k: port)
    device = DaqifiStreamingDevice(
        name="d", transport=transport_mod.SerialStreamTransport("COM58")
    )
    device.connect()
    _assert_close_mid_send_is_typed(device, port.entered, port.release)


def test_a_send_cut_off_by_close_is_typed_on_tcp(monkeypatch) -> None:
    import socket

    import daqifi.transport as transport_mod
    from daqifi._internal import scpi_commands

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    entered, release = threading.Event(), threading.Event()

    def _blocking_write(self, data):
        entered.set()
        release.wait(5)
        raise OSError("write on a closed socket")

    monkeypatch.setattr(transport_mod._SocketStream, "write", _blocking_write)
    transport = transport_mod.TcpStreamTransport("127.0.0.1", server.getsockname()[1])
    device = DaqifiStreamingDevice(name="d", transport=transport)
    try:
        device.connect()
        peer, _ = server.accept()
        try:
            _assert_close_mid_send_is_typed(device, entered, release)
        finally:
            peer.close()
    finally:
        release.set()
        server.close()


def _assert_close_mid_send_is_typed(device, entered, release) -> None:
    from daqifi._internal import scpi_commands

    outcome = {}

    def _send():
        try:
            device.send(scpi_commands.get_last_error())
        except BaseException as e:
            outcome["error"] = e

    sender = threading.Thread(target=_send, daemon=True)
    try:
        sender.start()
        assert entered.wait(5)
        device.close()
        release.set()
        sender.join(5)
        error = outcome.get("error")
        assert type(error).__name__ == "DeviceNotConnectedError", repr(error)
        assert error.is_shutting_down
    finally:
        release.set()


@pytest.mark.parametrize(
    "build",
    [
        lambda: exc_mod.DeviceNotConnectedError("gone", is_shutting_down=True),
        lambda: exc_mod.ScpiInitializationError("refused", ["a", "b"], "-200,x"),
        lambda: exc_mod.InitializationUnconfirmedError("unknown", ["l"]),
        lambda: exc_mod.SerialPortConnectError(
            "COM41", exc_mod.SerialPortConnectFailure.IN_USE, "detail"
        ),
    ],
    ids=["not_connected", "scpi_init", "unconfirmed", "serial_connect"],
)
def test_the_new_exceptions_survive_pickling(build) -> None:
    # A multiprocessing worker that raises one sends it back pickled; one
    # that can't be rebuilt crashes the parent's result handler.
    import pickle

    original = build()
    copy = pickle.loads(pickle.dumps(original))
    assert type(copy) is type(original)
    assert str(copy) == str(original)
    assert copy.__dict__ == original.__dict__


# ---- 3: execute_text_command --------------------------------------------------


def test_a_text_exchange_is_capped_while_the_device_keeps_talking() -> None:
    stream = _Stream()
    device = _device(stream)
    done = threading.Event()

    def _chatter():
        end = time.monotonic() + 6
        while not done.is_set() and time.monotonic() < end:
            stream.feed(b"line\r\n")
            time.sleep(0.05)

    feeder = threading.Thread(target=_chatter, daemon=True)
    try:
        feeder.start()
        began = time.monotonic()
        lines = device.execute_text_command(
            lambda: None, response_timeout_ms=200, completion_timeout_ms=300
        )
        elapsed = time.monotonic() - began
        assert lines
        assert elapsed < 2.0, f"took {elapsed:.2f} s; upstream ends it at 1.0 s"
    finally:
        done.set()
        feeder.join(7)
        device.disconnect()


def test_a_bare_lf_reply_is_read() -> None:
    from daqifi._internal.scpi_commands import ScpiMessage

    device = _device(_Stream(on_write={b"X?": b"value\n"}))
    try:
        began = time.monotonic()
        lines = device.execute_text_command(
            lambda: device.send(ScpiMessage("X?")),
            response_timeout_ms=2000, completion_timeout_ms=200,
        )
        assert lines == ["value"]
        assert time.monotonic() - began < 1.5
    finally:
        device.disconnect()


def test_a_blank_only_reply_ends_the_wait_and_returns_nothing() -> None:
    from daqifi._internal.scpi_commands import ScpiMessage

    device = _device(_Stream(on_write={b"SYSTem:LOG?": b"\r\n"}))
    try:
        began = time.monotonic()
        lines = device.execute_text_command(
            lambda: device.send(ScpiMessage("SYSTem:LOG?")),
            response_timeout_ms=3000, completion_timeout_ms=200,
        )
        assert lines == []
        assert time.monotonic() - began < 1.5
    finally:
        device.disconnect()


# ---- 4: typed errors -------------------------------------------------------


@pytest.mark.parametrize(
    "error, platform, reason",
    [
        (serial.SerialException(errno.ENOENT, "could not open port: [Errno 2] No such file"), "linux", "NOT_FOUND"),
        (serial.SerialException(errno.EBUSY, "could not open port: [Errno 16] Device or resource busy"), "linux", "IN_USE"),
        (serial.SerialException(errno.EAGAIN, "Could not exclusively lock port"), "linux", "IN_USE"),
        (serial.SerialException(errno.EACCES, "could not open port: [Errno 13] Permission denied"), "linux", "ACCESS_DENIED"),
        (serial.SerialException("could not open port 'COM41': PermissionError(13, 'Access is denied.', None, 5)"), "win32", "IN_USE"),
        (serial.SerialException("could not open port 'COM41': FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)"), "win32", "NOT_FOUND"),
        (serial.SerialException("could not open port 'COM41': OSError(22, 'The semaphore timeout period has expired.', None, 121)"), "win32", "UNKNOWN"),
    ],
)
def test_a_serial_connect_failure_is_named(monkeypatch, error, platform, reason) -> None:
    import sys

    import daqifi.transport as transport_mod

    def _fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(transport_mod.serial, "Serial", _fail)
    monkeypatch.setattr(sys, "platform", platform)
    t = transport_mod.SerialStreamTransport("COM41")
    with pytest.raises(serial.SerialException) as info:
        t.connect()
    raised = info.value
    assert type(raised).__name__ == "SerialPortConnectError"
    assert raised.reason.name == reason
    assert "COM41" in str(raised)
    assert raised.__cause__ is error


def test_nyquist_device_open_failure_names_the_cause(monkeypatch) -> None:
    import daqifi._internal.comm as comm_mod
    from daqifi.device import NyquistDevice

    def _busy(*args, **kwargs):
        raise serial.SerialException(errno.EBUSY, "could not open port COM50: busy")

    monkeypatch.setattr(comm_mod.serial, "Serial", _busy)
    with pytest.raises(exc_mod.ConnectionError) as info:
        NyquistDevice.connect("COM50")
    assert "in use" in str(info.value)
    assert isinstance(info.value.__cause__, serial.SerialException)


def test_not_connected_guards_raise_the_typed_error() -> None:
    from daqifi._internal import scpi_commands

    device = DaqifiStreamingDevice(name="d", transport=None)
    for call in (
        lambda: device.send(scpi_commands.get_last_error()),
        device.initialize,
        device.get_sd_card_files,
    ):
        with pytest.raises(RuntimeError) as info:
            call()
        assert type(info.value).__name__ == "DeviceNotConnectedError"
        assert not info.value.is_shutting_down


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"connection_timeout": -1.0},
        {"connection_timeout": 0.0},
        {"initial_delay": -0.5},
        {"max_delay": -1.0},
        {"backoff_multiplier": 0.5},
        {"connection_timeout": float("nan")},
    ],
)
def test_invalid_retry_options_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        ConnectionRetryOptions(**kwargs)
    options = ConnectionRetryOptions()
    (name, value), = kwargs.items()
    with pytest.raises(ValueError):
        setattr(options, name, value)


def test_valid_retry_options_still_work() -> None:
    for options in (
        ConnectionRetryOptions(),
        ConnectionRetryOptions.no_retry(),
        ConnectionRetryOptions.fast(),
        ConnectionRetryOptions.resilient(),
        ConnectionRetryOptions(initial_delay=0, max_delay=0, backoff_multiplier=1),
    ):
        assert options.max_attempts >= 1
