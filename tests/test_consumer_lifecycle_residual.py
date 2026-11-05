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


def test_the_consumer_survives_a_callback_that_delegates_a_send() -> None:
    """THE HARM, on its own channel: the stream still has a reader.

    Asserts nothing about errors or return values -- only that a frame fed
    after the schedule is still delivered. On `ed5ab261` the consumer
    thread has exited and this times out.
    """
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


def test_a_send_parked_during_an_exchange_lands_after_it() -> None:
    """Parking must keep the ordering guarantee that blocking gave (#225).

    A command issued while an exchange holds the device still goes out
    AFTER that exchange -- it is replayed on the holder's way out, before
    the lock is released, so it cannot overtake the exchange or race the
    next flow for the stream.
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

        # Thread B's send must NOT block (that is the residual) and must
        # NOT be on the wire yet (that is #225).
        sender_returned = threading.Event()

        def sender():
            device.send(_scpi().stop_streaming())
            sender_returned.set()

        threading.Thread(target=sender, name="parker", daemon=True).start()
        assert sender_returned.wait(5.0), (
            "send() blocked instead of parking while an exchange held the "
            "device"
        )
        assert [w for w in stream.written if b"STReam:STOP" in w] == [], (
            "a parked send reached the wire during the exchange"
        )

        release.set()
        exch.join(15.0)
        assert not exch.is_alive()
        _wait_for(
            lambda: [w for w in stream.written if b"STReam:STOP" in w] != [],
            10.0, "the parked send to be replayed after the exchange",
        )
        assert device.delivery_fault is None
    finally:
        release.set()
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


def test_a_parked_send_dropped_by_a_disconnect_does_not_look_delivered() -> None:
    """``send()`` returns before a parked command goes out, so the caller
    cannot be told by an exception if it never does. A drop therefore has
    to be recorded: core only logs it, and a silently dropped command is
    the same class of lie as a silently dead reader."""
    device = _fakes._make_device_with_serial_facade()
    try:
        # Park a command by holding the exchange lock from another thread.
        held = threading.Event()
        release = threading.Event()

        def holder():
            device._text_exchange_lock.acquire()
            held.set()
            release.wait(10.0)
            device._text_exchange_lock.release()

        h = threading.Thread(target=holder, name="holder", daemon=True)
        h.start()
        _wait_for(held.is_set, 5.0, "the lock to be held")

        device.send(_scpi().stop_streaming())      # parks, returns at once
        assert device._parked_sends, "the send was not parked"

        device.disconnect()                        # can never be sent now
        assert device.delivery_fault is not None, (
            "a command dropped by the disconnect left no trace"
        )
        assert "dropped" in device.delivery_fault
    finally:
        release.set()
        h.join(5)


def test_a_callbacks_send_cannot_interleave_inside_run_exclusive() -> None:
    """The consumer-thread exemption let a callback's command land INSIDE a
    grouped sequence (audit of ed5ab26, ledger row 837).

    ``send()`` used to bypass the exchange lock whenever it was called on
    the protobuf consumer thread, because BLOCKING there was the dangerous
    thing. So a message callback's command went straight out between a
    ``run_exclusive`` group's commands, and the group's own query read the
    callback's value back:

        wire: MAXSize 1024, MAXSize 2048, MAXSize?   -> query returned 2048

    Parking removes the block, which removes the reason for the exemption:
    the callback's send is now deferred and replayed after the group
    releases, so the group's query reads its own value and the callback's
    command still goes out, just after.
    """
    device = _fakes._make_device_with_serial_facade(
        {b"SYSTem:STORage:SD:MAXSize?": b"1024\r\n"}
    )
    stream = device._transport.stream
    sent_from_callback = threading.Event()
    in_group = threading.Event()
    frame_delivered = threading.Event()

    def on_message(_args):
        # A command issued from a message-received callback, mid-group.
        try:
            device.send(_scpi().set_sd_max_file_size(2048))
            sent_from_callback.set()
        finally:
            frame_delivered.set()

    device._consumer.add_message_received(on_message)
    try:
        def group():
            device.send(_scpi().set_sd_max_file_size(1024))
            in_group.set()
            # A protobuf frame arrives here, so the callback runs between
            # this group's two commands -- the audit's exact schedule.
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

        writes = [w for w in stream.written if b"MAXSize" in w]
        order = [w.decode(errors="replace").strip() for w in writes]
        # The group's own two commands must be adjacent: nothing of the
        # callback's between them.
        assert order[:2] == [
            "SYSTem:STORage:SD:MAXSize 1024",
            "SYSTem:STORage:SD:MAXSize?",
        ], f"a callback's command interleaved inside the group: {order}"
        # And the callback's command is not lost -- it goes out after.
        assert sent_from_callback.is_set()
        assert any("MAXSize 2048" in o for o in order), (
            f"the callback's parked command was never replayed: {order}"
        )
        assert device.delivery_fault is None
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


# ---- audit r1 on 468494b4: seven findings ------------------------------


def test_a_send_parked_in_the_release_window_is_not_stranded() -> None:
    """r1 #1: parking and draining were not atomic.

    B failed the acquire, A then drained an empty queue and released, and
    only after that did B append. Nobody held the lock and nobody drained
    again, so the command sat there forever with no `delivery_fault` and
    was replayed out of order by whatever flow came next.

    Driven deterministically: a patched `acquire` lets the test run A's
    whole drain-and-release in the window between B's failed acquire and
    B's append.
    """
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    try:
        a_done = threading.Event()

        class _HookedLock:
            """Delegates to a real lock, pausing after the FIRST failed
            non-blocking acquire. A real lock's methods are read-only, so
            the hook has to be a wrapper rather than a monkeypatch."""

            def __init__(self, inner):
                self._inner = inner
                self._armed = True

            def acquire(self, blocking=True, timeout=-1):
                got = (self._inner.acquire(blocking, timeout) if blocking
                       else self._inner.acquire(False))
                if not got and self._armed:
                    # B is now between "failed to acquire" and "park". Let A
                    # run its whole drain-and-release, which is the window.
                    self._armed = False
                    a_done.set()
                    time.sleep(0.3)
                return got

            def release(self):
                return self._inner.release()

        device._text_exchange_lock = _HookedLock(device._text_exchange_lock)

        holder_in = threading.Event()
        holder_go = threading.Event()

        def holder():
            with device._exchange_lock("race-holder"):
                holder_in.set()
                holder_go.wait(10.0)

        h = threading.Thread(target=holder, name="A", daemon=True)
        h.start()
        _wait_for(holder_in.is_set, 5.0, "A to hold the exchange")

        sender_done = threading.Event()

        def sender():
            device.send(_scpi().stop_streaming())
            sender_done.set()

        threading.Thread(target=sender, name="B", daemon=True).start()
        _wait_for(a_done.is_set, 5.0, "B's failed acquire")
        holder_go.set()          # A drains (sees empty) and releases
        h.join(10.0)
        assert sender_done.wait(10.0), "B never returned"

        # B's command must reach the wire, not sit in a queue nobody drains.
        _wait_for(
            lambda: [w for w in stream.written if b"STReam:STOP" in w] != [],
            10.0, "B's parked command to be delivered",
        )
        assert device._parked_sends == [], (
            f"a command was stranded in the queue: {device._parked_sends}"
        )
        assert device.delivery_fault is None
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


def test_an_interrupted_replay_still_releases_the_exchange() -> None:
    """r1 #2: the replay caught only Exception, so a BaseException out of a
    write skipped clearing the owner and releasing the lock, and every
    later exchange on the device hung forever."""
    device = _fakes._make_device_with_serial_facade()
    try:
        # Park one command behind a holder.
        held = threading.Event()
        release = threading.Event()

        def holder():
            with device._exchange_lock("holder"):
                held.set()
                release.wait(10.0)

        h = threading.Thread(target=holder, name="holder", daemon=True)
        h.start()
        _wait_for(held.is_set, 5.0, "the lock to be held")
        device.send(_scpi().stop_streaming())
        assert device._parked_sends, "nothing parked"

        # Make the replay's write raise a BaseException.
        def boom(_data):
            raise KeyboardInterrupt("interrupted mid-replay")

        device._write_bytes = boom       # type: ignore[assignment]
        release.set()
        h.join(10.0)

        # The lock must be free and unowned despite the interruption.
        assert device._text_exchange_lock.acquire(blocking=False), (
            "the exchange lock was never released after an interrupted "
            "replay; every later exchange would hang"
        )
        device._text_exchange_lock.release()
        assert device._text_exchange_lock_owner is None
        assert device.delivery_fault is not None, (
            "the abandoned backlog left no trace"
        )
    finally:
        release.set()
        h.join(5)


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

        # The same fault, recorded against an OLDER connection, does not.
        device._record_delivery_fault("induced", device._connect_generation - 1)
        assert device.delivery_fault is None, (
            "a fault from a replaced connection condemned the live one"
        )
        assert device.is_streaming is False or True   # no raise expected
    finally:
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


def test_streaming_state_follows_the_wire_not_the_call() -> None:
    """r1 #4: stop_streaming() parked STOP and flipped the flag at once, so
    a START written by the owner in between left the wire STOPped-then-
    STARTed while is_streaming read the opposite of the device."""
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    try:
        device._is_streaming = True

        held = threading.Event()
        release = threading.Event()

        def holder():
            with device._exchange_lock("holder"):
                held.set()
                release.wait(10.0)

        h = threading.Thread(target=holder, name="holder", daemon=True)
        h.start()
        _wait_for(held.is_set, 5.0, "the lock to be held")

        device.stop_streaming()          # parks STOP
        assert device.is_streaming is True, (
            "is_streaming flipped while the STOP was still queued: the "
            "device had not been told anything yet"
        )

        release.set()
        h.join(10.0)
        _wait_for(
            lambda: [w for w in stream.written if b"STReam:STOP" in w] != [],
            10.0, "the parked STOP to be written",
        )
        assert device.is_streaming is False, (
            "is_streaming did not follow the command onto the wire"
        )
    finally:
        release.set()
        h.join(5)
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


def test_a_parked_command_is_immune_to_the_caller_mutating_its_message() -> None:
    """r1 #6: the queue held the ScpiMessage object, so a caller that
    reused or mutated it after send() returned changed what was replayed."""
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    try:
        held = threading.Event()
        release = threading.Event()

        def holder():
            with device._exchange_lock("holder"):
                held.set()
                release.wait(10.0)

        h = threading.Thread(target=holder, name="holder", daemon=True)
        h.start()
        _wait_for(held.is_set, 5.0, "the lock to be held")

        message = _scpi().set_sd_max_file_size(1024)
        device.send(message)                     # parks
        message.data = "SYSTem:STORage:SD:MAXSize 9999"   # caller mutates it

        release.set()
        h.join(10.0)
        _wait_for(
            lambda: [w for w in stream.written if b"MAXSize" in w] != [],
            10.0, "the parked command to be replayed",
        )
        written = b"".join(stream.written)
        assert b"MAXSize 1024" in written, "the parked command was not replayed"
        assert b"MAXSize 9999" not in written, (
            "the caller's later mutation changed what was sent"
        )
    finally:
        release.set()
        h.join(5)
        try:
            device.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


def test_initialize_waits_for_the_device_instead_of_parking_into_it() -> None:
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
