"""The consumer must never die silently under a text exchange (#238 residual).

Carried forward from the exchange-serialization PR as a must-fix. The
schedule, which the fourth review round on `9ae955fe` reproduced:

1. a message-received callback is in flight on the consumer thread;
2. a text exchange starts on another thread, takes the exchange lock, and
   asks the consumer to stop -- the join TIMES OUT, because the consumer
   thread is inside that callback, and the capture goes ahead anyway;
3. the callback delegates a ``send()`` to a worker and waits for it; the
   worker blocks on the exchange lock the exchange still holds;
4. the exchange's restart raises "previous consumer thread is still alive"
   and ``execute_text_command``'s ``finally`` swallows it;
5. the lock is released, the worker completes, the callback returns, and
   the consumer's ``while self._is_running`` loop exits for good.

Afterwards ``start_streaming()`` reports success and no frames arrive.

The ordering here is deterministic, not raced. The callback PARKS before
the exchange starts, so the stop is guaranteed to find the consumer thread
inside it and time out; and the worker's ``send()`` is released only after
``_is_running`` has gone False, which proves the stop already ran. Without
that, the worker can win the lock first and the schedule never forms --
the same trap that made an earlier version of this repro pass against
defective code.
"""

import importlib.util
import os
import threading
import time

import pytest

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes_residual",
    os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py"),
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


def _frame(n: int = 2) -> bytes:
    from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage

    body = DaqifiOutMessage(analog_in_port_num=n, device_sn=1234).SerializeToString()
    return bytes([len(body)]) + body


def _scpi():
    from daqifi._internal import scpi_commands

    return scpi_commands


def _wait_for(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.005)


class _Schedule:
    """Drives the exact r4 interleaving and records what happened."""

    def __init__(self, device):
        self.device = device
        self.in_callback = threading.Event()
        self.release_callback = threading.Event()
        self.callback_done = threading.Event()
        self.later_frames = 0
        self.worker_outcome = None
        self.exchange_done = threading.Event()
        self.exchange_error = None

    def on_message(self, _args):
        if self.in_callback.is_set():
            # Any frame after the first: this is the liveness signal.
            self.later_frames += 1
            return
        self.in_callback.set()
        # Park here so the exchange's consumer-stop is guaranteed to find
        # this thread inside the callback.
        self.release_callback.wait(20.0)
        # Now delegate work to another thread and wait for it, which is
        # what a recovery handler does.
        def worker():
            try:
                self.device.send(_scpi().stop_streaming())
                self.worker_outcome = "sent"
            except BaseException as exc:                          # noqa: BLE001
                self.worker_outcome = f"{type(exc).__name__}: {exc}"

        w = threading.Thread(target=worker, name="delegated-send", daemon=True)
        w.start()
        w.join(20.0)
        self.callback_done.set()

    def run_exchange(self):
        try:
            self.device.execute_text_command(
                lambda: None, response_timeout_ms=150, completion_timeout_ms=50
            )
        except BaseException as exc:                              # noqa: BLE001
            self.exchange_error = exc
        finally:
            self.exchange_done.set()


def _run_r4_schedule():
    """Returns (device, schedule) after the whole interleaving has played out."""
    device = _fakes._make_device_with_serial_facade()
    sched = _Schedule(device)
    device._consumer.add_message_received(sched.on_message)

    # 1. Get the consumer thread parked inside a callback.
    device._transport.stream.feed(_frame())
    _wait_for(sched.in_callback.is_set, 10.0, "the callback to start")

    # 2. Start the exchange. It takes the lock and asks the consumer to
    #    stop; the join must time out, because the consumer is parked above.
    exch = threading.Thread(target=sched.run_exchange, name="exchange", daemon=True)
    exch.start()

    # 3. Wait for proof the stop ran: stop_safely clears _is_running before
    #    it joins, so this flipping False means the exchange is past it.
    _wait_for(
        lambda: not device._consumer.is_running, 10.0,
        "the exchange's consumer-stop to run",
    )

    # 4. Only now release the callback, so the delegated send is guaranteed
    #    to meet a held lock rather than racing the exchange for it.
    sched.release_callback.set()

    sched.exchange_done.wait(25.0)
    sched.callback_done.wait(25.0)
    return device, sched


@pytest.mark.xfail(
    reason="PRE-EXISTING ON MAIN, and left unfixed by ruling EI B. A callback "
           "that delegates a send to another thread and JOINS it blocks the "
           "consumer thread by its own means: the worker waits for the "
           "exchange, the callback waits for the worker, and the exchange "
           "cannot restart the consumer until the callback returns, so the "
           "restart fails. Verified to fail identically on fbbc688, so it is "
           "not a regression from the blocking rework -- parking was what "
           "fixed it, and EI B removed parking. send()'s refusal covers a "
           "DIRECT send from the consumer thread; the library cannot see "
           "through a join(). Asserts the CORRECT behaviour, so it never "
           "requires the defect and XPASSes if anyone fixes it.",
    strict=False,
)
def test_the_consumer_survives_a_callback_that_delegates_a_send() -> None:
    """THE HARM, on its own channel: the stream still has a reader."""
    device, sched = _run_r4_schedule()
    try:
        assert sched.exchange_done.is_set(), "the exchange never finished"
        assert sched.callback_done.is_set(), "the callback never finished"
        before = sched.later_frames
        device._transport.stream.feed(_frame(3))
        _wait_for(
            lambda: sched.later_frames > before, 10.0,
            "a frame to be delivered after the schedule",
        )
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass

def test_streaming_is_not_reported_when_the_restart_failed() -> None:
    """THE LIE, on its own channel, and induced rather than hoped for.

    The earlier version of this test ran the r4 schedule and then checked
    the reporting -- but once the schedule is fixed the reader survives, so
    it SKIPPED, and a skip is not evidence. A failed restart is forced here
    instead, so the reporting claim is tested on its own: whether or not any
    schedule can still kill the reader, nothing may report streaming over
    a reader that is gone.
    """
    from daqifi.exceptions import DeliveryFaultError

    device = _fakes._make_device_with_serial_facade()
    try:
        # Force the restart after the exchange's consumer swap to fail.
        def refuse_start():
            raise RuntimeError("induced: previous reader still alive")

        device._consumer.start = refuse_start

        device.execute_text_command(
            lambda: None, response_timeout_ms=100, completion_timeout_ms=50
        )

        # The exchange itself still returns -- its captured reply is the
        # caller's result and must not be discarded over this.
        assert device.delivery_fault is not None, (
            "a failed consumer restart left no trace on the device; on "
            "ed5ab261 it was swallowed by `except Exception: pass`"
        )
        assert "restart" in device.delivery_fault

        # And the two calls that used to misreport now cannot.
        with pytest.raises(DeliveryFaultError):
            device.start_streaming()
        assert device.is_streaming is False
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


def test_a_send_waits_for_an_exchange_and_lands_after_it() -> None:
    """#225's ordering guarantee, now kept by WAITING (ruling EI B).

    A command issued while an exchange holds the device goes out after that
    exchange. Parking achieved that by replaying on the holder's way out;
    waiting achieves it directly, and keeps send()'s contract -- returning
    means the bytes are on the wire, which is what every caller that pairs
    a send with a state change or a deadline assumes.

    The assertions are the INVERSE of the parking ones: the sender must NOT
    return early, and must complete once the exchange ends.
    """
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    try:
        in_exchange = threading.Event()
        release = threading.Event()

        def setup_action():
            in_exchange.set()
            release.wait(10.0)

        exch = threading.Thread(
            target=lambda: device.execute_text_command(
                setup_action, response_timeout_ms=100, completion_timeout_ms=50
            ),
            name="exchange", daemon=True,
        )
        exch.start()
        _wait_for(in_exchange.is_set, 10.0, "the exchange to start")

        sender_returned = threading.Event()

        def sender():
            device.send(_scpi().stop_streaming())
            sender_returned.set()

        threading.Thread(target=sender, name="waiter", daemon=True).start()
        assert not sender_returned.wait(1.0), (
            "send() returned while an exchange still held the device, so it "
            "did not wait -- returning must mean the bytes are out"
        )
        assert [w for w in stream.written if b"STReam:STOP" in w] == [], (
            "a waiting send reached the wire during the exchange"
        )

        release.set()
        exch.join(15.0)
        assert not exch.is_alive()
        assert sender_returned.wait(10.0), (
            "the waiting send never completed after the exchange released"
        )
        assert [w for w in stream.written if b"STReam:STOP" in w] != [], (
            "the send completed without reaching the wire"
        )
        assert device.delivery_fault is None
    finally:
        release.set()
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass

def test_a_callbacks_send_is_refused_rather_than_interleaving() -> None:
    """The consumer-thread exemption let a callback's command land INSIDE a
    grouped sequence (audit of ed5ab26, ledger row 837).

    ``send()`` used to bypass the exchange lock whenever it was called on
    the protobuf consumer thread, because BLOCKING there was the dangerous
    thing. So a message callback's command went straight out between a
    ``run_exclusive`` group's commands, and the group's own query read the
    callback's value back:

        wire: MAXSize 1024, MAXSize 2048, MAXSize?   -> query returned 2048

    Under ruling EI B the callback neither blocks nor defers: it is
    REFUSED. The ordering property is kept more strictly than parking kept
    it -- nothing can interleave -- and the trade is explicit, because the
    command does NOT go out and the caller is told so rather than being
    given a silent deferral.
    """
    device = _fakes._make_device_with_serial_facade(
        {b"SYSTem:STORage:SD:MAXSize?": b"1024\r\n"}
    )
    stream = device._transport.stream
    in_group = threading.Event()
    frame_delivered = threading.Event()
    callback_outcome = []

    def on_message(_args):
        try:
            device.send(_scpi().set_sd_max_file_size(2048))
            callback_outcome.append("sent")
        except BaseException as exc:                              # noqa: BLE001
            callback_outcome.append(type(exc).__name__)
        finally:
            frame_delivered.set()

    device._consumer.add_message_received(on_message)
    try:
        def group():
            device.send(_scpi().set_sd_max_file_size(1024))
            in_group.set()
            assert frame_delivered.wait(10.0), "the callback never ran"
            return device.execute_text_command(
                lambda: device.send(_scpi().get_sd_max_file_size()),
                response_timeout_ms=200, completion_timeout_ms=60,
            )

        runner = threading.Thread(
            target=lambda: setattr(runner, "result", device.run_exclusive(group)),
            name="group", daemon=True,
        )
        runner.result = None
        runner.start()
        _wait_for(in_group.is_set, 10.0, "the group's first command")
        stream.feed(_frame())
        runner.join(25.0)
        assert not runner.is_alive(), "the group never finished"

        order = [w.decode(errors="replace").strip()
                 for w in stream.written if b"MAXSize" in w]
        assert order[:2] == [
            "SYSTem:STORage:SD:MAXSize 1024",
            "SYSTem:STORage:SD:MAXSize?",
        ], f"a callback's command interleaved inside the group: {order}"

        # ⭐ The callback was REFUSED, promptly and by name. It must not
        # have blocked (that is the #238 deadlock) and must not have
        # reached the wire (that is #225).
        assert callback_outcome == ["ExchangeTimeoutError"], (
            f"the callback's send was not refused: {callback_outcome}"
        )
        assert not any("MAXSize 2048" in o for o in order), (
            f"a refused command reached the wire anyway: {order}"
        )
        assert device.delivery_fault is None
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass

# ---- audit r1 on 468494b4: seven findings ------------------------------


def test_a_failing_send_still_releases_the_exchange() -> None:
    """A send that raises must not leave the device locked.

    Under parking this was about an interrupted replay; the surviving
    property is simpler and still essential -- the release is in a
    ``finally``, so a write that throws cannot wedge every later exchange.
    """
    device = _fakes._make_device_with_serial_facade()
    try:
        def boom(_data):
            raise IOError("stream exploded")

        device._transport.stream.write = boom
        with pytest.raises(Exception):
            device.send(_scpi().stop_streaming())

        assert device._text_exchange_lock_owner is None, (
            "a failed send left itself recorded as the exchange owner"
        )
        assert device._text_exchange_lock.acquire(blocking=False), (
            "a failed send left the exchange lock held, so every later "
            "exchange on this device would block"
        )
        device._text_exchange_lock.release()
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass

def test_a_fault_on_an_old_connection_does_not_condemn_the_new_one() -> None:
    """r1 #3: a replay that failed on connection A set the fault globally,
    so a reconnected device -- Connected, healthy, a fresh reader -- raised
    DeliveryFaultError from start_streaming()."""
    from daqifi.exceptions import DeliveryFaultError

    device = _fakes._make_device_with_serial_facade()
    try:
        # A fault recorded against the CURRENT connection applies.
        device._record_delivery_fault("induced", device._connect_generation)
        assert device.delivery_fault is not None
        with pytest.raises(DeliveryFaultError):
            device.start_streaming()

        # ⚠ The second half of this test was restated for round 3's
        # compare-and-set. It used to record an older-generation fault and
        # assert it was invisible -- which worked because the record
        # OVERWROTE, leaving a stale generation. Overwriting was itself the
        # r3 #3 defect: a stale drain could clear a live fault and
        # start_streaming() then succeeded over a dead consumer. An older
        # fault is now simply not recorded, which satisfies this test's
        # intent more strongly, so the intent is asserted directly on a
        # fresh device instead.
        other = _fakes._make_device_with_serial_facade()
        try:
            other._record_delivery_fault(
                "a failure on a connection since replaced",
                other._connect_generation - 1,
            )
            assert other.delivery_fault is None, (
                "a fault from a replaced connection condemned the live one"
            )
            other.start_streaming()      # must not raise
        finally:
            try:
                other.disconnect()
            except Exception:                                     # noqa: BLE001
                pass
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


def test_streaming_state_follows_the_wire_not_the_call() -> None:
    """``is_streaming`` must not be true before the START is on the wire.

    Parking forced this to be bound to a write callback. Blocking makes the
    plain order correct -- send() returns only once the bytes are out -- but
    the property still needs pinning, because the state assignment sitting
    BEFORE the send would be an easy and invisible edit.
    """
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    seen_at_write = []
    real_write = stream.write

    def watching_write(data):
        if b"STReam:START" in bytes(data):
            seen_at_write.append(device.is_streaming)
        return real_write(data)

    stream.write = watching_write
    try:
        device.streaming_frequency = 100
        device.start_streaming()
        assert seen_at_write == [False], (
            "is_streaming was already True when the START reached the "
            f"stream, so the state leads the wire: {seen_at_write}"
        )
        assert device.is_streaming is True, (
            "is_streaming was never set after the START went out"
        )
    finally:
        stream.write = real_write
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass

def test_initialize_owns_the_device_for_its_whole_sequence() -> None:
    """r1 #7: initialize() started its response deadline while its own
    commands were still parked behind another flow, so it timed out and
    unregistered its handler before `SYSInfoPB?` had even been written.

    It now OWNS the exchange for the whole sequence, which is what the
    sequence assumed all along. Observable without timing games: while
    another flow holds the device, initialize must have written NOTHING --
    if it were parking it would have queued all six commands and moved on
    to its deadline.
    """
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    held = threading.Event()
    release = threading.Event()
    init_done = threading.Event()

    def holder():
        with device._exchange_lock("holder"):
            held.set()
            release.wait(10.0)

    h = threading.Thread(target=holder, name="holder", daemon=True)
    try:
        h.start()
        _wait_for(held.is_set, 5.0, "the lock to be held")
        writes_before = len(stream.written)

        def do_init():
            try:
                device.initialize(response_timeout_s=0.3)
            except Exception:                                     # noqa: BLE001
                pass
            finally:
                init_done.set()

        threading.Thread(target=do_init, name="init", daemon=True).start()
        time.sleep(0.6)      # twice the response timeout

        assert not init_done.is_set(), (
            "initialize() ran to completion while another flow held the "
            "device: its commands were parked and its deadline expired "
            "before they were ever written"
        )
        assert stream.written[writes_before:] == [], (
            "initialize() queued commands instead of waiting for the device"
        )

        release.set()
        h.join(10.0)
        assert init_done.wait(15.0), "initialize() never finished"
        assert [w for w in stream.written if b"ECHO" in w or b"SYSInfoPB" in w], (
            "initialize() wrote nothing even after the device was free"
        )
    finally:
        release.set()
        h.join(5)
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass

# r1 #5 (a text capture robbed by a reader that had not exited) has NO test
# here, deliberately, and that is a gap rather than a claim of coverage.
#
# The fix is a REVERT: an earlier revision of this branch removed the second
# bounded join in _execute_text_command_locked, reasoning that stop() "can
# neither join itself nor force the exit". That is true only when the caller
# IS the reader; from any other thread stop() joins for another second, and
# the audit demonstrated empirically that that second is what stops the old
# reader consuming the text reply (it returned [] instead of the answer).
#
# Two constructions were tried here and NEITHER discriminated: one where the
# stalled read returns the data (the reader consumes it either way, so the
# second join cannot help) and one where it times out (the capture succeeds
# on the defective head too). A test that passes on both sides is not a
# control, so it is not here. The evidence for this fix is the audit's own
# reproduction plus the pre-existing behaviour it restores.


# ---- audit round 2 on 018e93a9: four findings ---------------------------


def test_a_command_a_delay_depends_on_owns_the_exchange(tmp_path) -> None:
    """r2 #1: the firmware updater sent FORceBoot, slept, then disconnected
    WITHOUT owning the device's exchange. Held by another flow, FORceBoot
    parked and the disconnect discarded it: the board stayed in application
    mode and the update timed out waiting for a bootloader nobody asked for.

    The updater's own _operation_lock does not help -- it serialises
    updates against each other, not against the device's other flows.
    """
    from daqifi.firmware.update_service import DefaultFirmwareUpdateService

    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    held = threading.Event()
    release = threading.Event()

    def holder():
        with device._exchange_lock("other-flow"):
            held.set()
            release.wait(10.0)

    h = threading.Thread(target=holder, name="holder", daemon=True)
    try:
        h.start()
        _wait_for(held.is_set, 5.0, "another flow to hold the exchange")

        service = DefaultFirmwareUpdateService.__new__(
            DefaultFirmwareUpdateService
        )
        # The helper is the whole fix: it owns the exchange for the send so
        # the delay that follows measures time AFTER delivery.
        service._send_and_settle(
            device, _scpi().force_bootloader(), 0, None
        )

        assert [w for w in stream.written if b"FORceBoot" in w] != [], (
            "the command the following delay depends on was parked: a "
            "disconnect after the delay would discard it entirely"
        )
    finally:
        release.set()
        h.join(5)
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


def test_a_waiting_send_is_refused_when_the_link_goes_while_it_waits() -> None:
    """The post-acquire re-check, which the wait makes necessary.

    A send validates the connection on the way in, then waits for whatever
    holds the device -- and that holder may be the thing that loses the
    connection. Without a re-check after acquiring, the command goes onto a
    link already marked Lost, leaving the device Lost and Streaming at
    once. Parking is gone but the DELAY is not, so neither is this check:
    any mechanism that makes a send wait moves it past the checks it passed
    on the way in.
    """
    from daqifi._internal.scpi import ConnectionStatus

    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    holder_in = threading.Event()
    holder_go = threading.Event()

    def holder():
        with device._exchange_lock("A"):
            holder_in.set()
            holder_go.wait(10.0)
            # A loses the link while B is already waiting behind it.
            device._status = ConnectionStatus.Lost

    h = threading.Thread(target=holder, name="A", daemon=True)
    try:
        h.start()
        _wait_for(holder_in.is_set, 5.0, "A to hold the exchange")

        outcome = []
        waiting = threading.Event()

        def sender():
            waiting.set()
            try:
                device.send(_scpi().stop_streaming())
                outcome.append("sent")
            except BaseException as exc:                          # noqa: BLE001
                outcome.append(type(exc).__name__)

        threading.Thread(target=sender, name="B", daemon=True).start()
        assert waiting.wait(5.0)
        time.sleep(0.3)                  # B is now blocked on the lock
        assert outcome == [], f"B did not wait for the exchange: {outcome}"

        holder_go.set()
        h.join(10.0)
        _wait_for(lambda: outcome != [], 10.0, "B's send to resolve")

        assert outcome == ["DeviceNotConnectedError"], (
            f"a send that waited onto a lost link was not refused: {outcome}"
        )
        assert [w for w in stream.written if b"STReam:STOP" in w] == [], (
            "a refused command reached the wire anyway"
        )
    finally:
        holder_go.set()
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass

def test_closing_during_the_grace_wait_does_not_revive_the_reader() -> None:
    """r2 #3: close() during start()'s new grace-period join published a
    reader into a DISPOSED consumer. Every later close() then returned
    early as already-disposed, so nothing ever stopped that reader.

    The grace period introduced this window; the disposal re-check under
    the publish lock closes it.
    """
    from daqifi.communication import LineBasedMessageParser, StreamMessageConsumer

    class _SlowStream:
        """A read that blocks, so the reader cannot exit promptly."""

        def __init__(self):
            self.closed = False

        def read(self, _n):
            time.sleep(0.4)
            return b""

        @property
        def in_waiting(self):
            return 0

    consumer = StreamMessageConsumer[str](
        _SlowStream(), LineBasedMessageParser()
    )
    consumer.start()
    time.sleep(0.1)                      # the reader is inside a read
    consumer.stop_safely(timeout_ms=1)   # asked to stop, will not exit yet

    # close() lands while start() is in its grace join.
    def closer():
        time.sleep(0.05)
        consumer.close()

    threading.Thread(target=closer, daemon=True).start()
    try:
        consumer.start()
    except Exception:                                             # noqa: BLE001
        pass                             # refusing to start is a fine outcome

    _wait_for(lambda: consumer._disposed, 5.0, "the consumer to be disposed")
    # The invariant: a disposed consumer must not be left with a live reader.
    thread = getattr(consumer, "_thread", None)
    assert not (consumer._disposed and thread is not None and thread.is_alive()
                and consumer.is_running), (
        "a reader was published into a disposed consumer, so no later "
        "close() can ever stop it"
    )


def test_an_old_consumers_restart_failure_is_not_charged_to_the_new_one() -> None:
    """r2 #4: a restart failure was recorded against whatever generation
    existed when the attempt finished, so a reconnected device inherited a
    fault from the connection it replaced."""
    device = _fakes._make_device_with_serial_facade()
    try:
        # Force the restart after the consumer swap to fail, and let a
        # reconnect land while the exchange is still running -- so the
        # attempt finishes under a NEWER generation than it started under.
        def refuse_start():
            raise RuntimeError("induced: restart failed on the old link")

        device._consumer.start = refuse_start        # type: ignore[assignment]

        def reconnect_midway():
            device._connect_generation += 1

        device.execute_text_command(
            reconnect_midway, response_timeout_ms=100,
            completion_timeout_ms=50,
        )

        assert device.delivery_fault is None, (
            "a restart failure on the old connection was charged to the "
            f"replacement: {device.delivery_fault}"
        )
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


# ---- audit round 3 on d7a862e5: four findings ---------------------------


def test_two_concurrent_starts_publish_only_one_reader() -> None:
    """r3 #1: both start() calls passed the post-join is_alive() check and
    _is_running was not re-checked under the lifecycle lock, so BOTH
    published a reader. Two readers on one stream: callbacks saw A, B, A.

    ⚠ AN INTERMITTENT RACE DETECTOR, AND SAID SO DELIBERATELY. It asserts
    the invariant -- never two live readers on one stream -- which is what
    matters, and the invariant holds deterministically WITH the fix because
    the second caller then returns early. On the defective head the
    violation is only sometimes reachable: measured over three runs it
    failed once and passed twice, because the second caller often observes
    the first caller's freshly published reader and raises instead. So a
    single green run here is NOT proof the defect is absent; the
    deterministic evidence is the re-check itself.
    """
    from daqifi.communication import LineBasedMessageParser, StreamMessageConsumer

    class _SlowStream:
        def read(self, _n):
            time.sleep(0.3)
            return b""

        @property
        def in_waiting(self):
            return 0

    # ⚠ Snapshot first: threading.enumerate() is process-GLOBAL, and other
    # tests in this file leave consumers running. A bare name match counted
    # theirs and made this test order-dependent -- it passed alone and
    # failed in the file, which is the test's defect, not the code's.
    pre_existing = set(threading.enumerate())

    consumer = StreamMessageConsumer[str](_SlowStream(), LineBasedMessageParser())
    consumer.start()
    time.sleep(0.05)
    consumer.stop_safely(timeout_ms=1)       # will not have exited yet

    # Force the interleaving rather than hope for it: both starts must be
    # past the grace join at the same moment, which is the window.
    at_join = threading.Event()
    real_join = consumer._thread.join

    def hooked_join(timeout=None):
        at_join.set()
        time.sleep(0.35)          # hold both callers inside the join
        return real_join(timeout)

    consumer._thread.join = hooked_join      # type: ignore[assignment]

    threads = []
    errors = []

    # A barrier, so both callers are inside start() together. Without it
    # the first call can finish before the second begins, and the race the
    # test exists for never forms.
    gate = threading.Barrier(2, timeout=10)

    def starter():
        try:
            gate.wait()
            consumer.start()
        except Exception as exc:                                  # noqa: BLE001
            errors.append(type(exc).__name__)

    for _ in range(2):
        t = threading.Thread(target=starter, daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join(10)
    try:
        # However the race resolves, exactly one reader may be live.
        alive = [th for th in threading.enumerate()
                 if th not in pre_existing
                 and th.name.startswith("MessageConsumer-") and th.is_alive()]
        assert len(alive) <= 1, (
            f"{len(alive)} readers live on one stream: {[t.name for t in alive]}"
        )
    finally:
        consumer.close()


def test_a_bus_handoff_owns_the_exchange_for_both_its_commands() -> None:
    """r3 #2: prepare_sd_interface() sends two commands and sleeps twice for
    the SPI handoff. Parked, the sleeps ran before delivery and the two
    commands then replayed microseconds apart -- the firmware -200 risk.

    Owning the exchange makes both immediate. Asserted on the wire: with
    another flow holding the device, the pair must NOT appear until it can
    own the exchange, and must then appear together.
    """
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    held = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def holder():
        with device._exchange_lock("other-flow"):
            held.set()
            release.wait(10.0)

    h = threading.Thread(target=holder, name="holder", daemon=True)
    try:
        h.start()
        _wait_for(held.is_set, 5.0, "another flow to hold the device")
        before = len(stream.written)

        threading.Thread(
            target=lambda: (device.prepare_sd_interface(), done.set()),
            daemon=True,
        ).start()
        time.sleep(0.6)      # longer than both SPI settle delays together
        # ⭐ THE DISCRIMINATOR IS THAT IT HAS NOT RETURNED. Parking also
        # leaves the wire empty, so "nothing written yet" is true either
        # way and proves nothing. What parking does is let the call RUN TO
        # COMPLETION -- sleeping out both SPI settle delays and returning
        # -- while its two commands sit queued. Owning the exchange means
        # it cannot finish until they are actually out.
        assert not done.is_set(), (
            "prepare_sd_interface returned while another flow held the "
            "device: its settle delays ran before delivery, and its two "
            "commands would then replay microseconds apart"
        )
        assert stream.written[before:] == []

        release.set()
        h.join(10.0)
        assert done.wait(10.0), "the handoff never completed"
        written = b"".join(stream.written[before:])
        assert b"SD:ENAble 1" in written or b"STORage:SD" in written, (
            f"the handoff's commands never reached the wire: {written!r}"
        )
    finally:
        release.set()
        h.join(5)
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


def test_a_stale_fault_cannot_clear_a_live_one() -> None:
    """r3 #3: a late drain's fault overwrote the current generation's, and
    because the stored generation was then stale, delivery_fault read None
    and start_streaming() succeeded over a dead consumer."""
    from daqifi.exceptions import DeliveryFaultError

    device = _fakes._make_device_with_serial_facade()
    try:
        current = device._connect_generation
        device._record_delivery_fault("the live connection's fault", current)
        assert device.delivery_fault is not None

        # A stale drain finishing late, carrying an older generation.
        device._record_delivery_fault("a stale drain's fault", current - 1)

        assert device.delivery_fault is not None, (
            "a stale fault cleared the live one: start_streaming() would "
            "now succeed over a dead consumer"
        )
        assert "live connection" in device.delivery_fault
        with pytest.raises(DeliveryFaultError):
            device.start_streaming()
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass




# ---- ruling EI B: the bounded wait itself -------------------------------


def test_the_send_wait_covers_the_longest_exchange() -> None:
    """⛔ THE BOUND MUST NOT BE SHORTER THAN A LEGITIMATE HOLDER.

    ``_SEND_EXCHANGE_WAIT_S`` exists to turn a STUCK exchange into a clear
    error, not to cap a caller's patience. An SD download may legitimately
    hold the device for ``_SD_DOWNLOAD_TIMEOUT_S``, so a shorter bound
    would turn an ordinary wait behind one into a failure -- a regression
    dressed as a safety limit.

    The two live on different classes (``send`` is on ``DaqifiDevice``, the
    download timeout on ``DaqifiStreamingDevice``), so they cannot be
    derived from one expression; the relationship is asserted instead. If
    someone raises the download timeout, this fails and tells them.
    """
    from daqifi.devices import DaqifiDevice, DaqifiStreamingDevice

    assert DaqifiDevice._SEND_EXCHANGE_WAIT_S >= (
        DaqifiStreamingDevice._SD_DOWNLOAD_TIMEOUT_S
    ), (
        "the send wait is now shorter than an SD download may hold the "
        "device, so a send issued during a download would fail instead of "
        "waiting; raise _SEND_EXCHANGE_WAIT_S to cover it"
    )


def test_a_send_that_waits_too_long_is_told_so() -> None:
    """The expiry path: a clear error, and the command did NOT go out.

    The real bound is half an hour, so it is overridden per instance here.
    That is the point of reading it through ``self``.
    """
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    held = threading.Event()
    release = threading.Event()

    def holder():
        with device._exchange_lock("test-holder"):
            held.set()
            release.wait(10.0)

    h = threading.Thread(target=holder, name="holder", daemon=True)
    try:
        h.start()
        _wait_for(held.is_set, 5.0, "the exchange to be held")
        device._SEND_EXCHANGE_WAIT_S = 0.2

        from daqifi.exceptions import ExchangeTimeoutError

        started = time.monotonic()
        with pytest.raises(ExchangeTimeoutError) as caught:
            device.send(_scpi().stop_streaming())
        waited = time.monotonic() - started

        assert 0.15 <= waited < 5.0, (
            f"the send did not honour its own bound: waited {waited:.2f}s"
        )
        assert "NOT sent" in str(caught.value), (
            "the error does not tell the caller the command never went out"
        )
        assert [w for w in stream.written if b"STReam:STOP" in w] == [], (
            "a send that reported a timeout reached the wire anyway"
        )
        # ⚠ NOT `is None`: the holder is still holding, so it legitimately
        # owns the lock here. The property is that the send that gave up
        # did not record ITSELF -- doing so would let it nest an exchange
        # and would make it deliver status events under a lock it does not
        # hold. (My first version of this assertion was `is None` and
        # failed on the holder's own ownership.)
        assert device._text_exchange_lock_owner != threading.get_ident(), (
            "a timed-out send recorded itself as the exchange owner"
        )
    finally:
        release.set()
        h.join(5)
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass
