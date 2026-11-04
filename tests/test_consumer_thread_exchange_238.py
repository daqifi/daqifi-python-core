"""#238/#225: no text exchange runs on the protobuf consumer thread, and
send() is serialized against exchanges.

A text exchange pauses the protobuf consumer, swaps a text reader onto the
same stream, and restarts the consumer afterwards. Started from a
message-received callback, it is the consumer's own thread doing all that to
itself: the pause sets ``_is_running`` False, the restart raises "previous
consumer thread is still alive" and ``execute_text_command``'s ``finally``
swallows it, and the call RETURNS NORMALLY while the consumer's
``while self._is_running`` loop exits the moment the callback does. Nothing
reads the stream after that, and a later ``start_streaming()`` still reports
streaming (#238).

Main refuses exactly one case: an exchange from the callback *while another
exchange holds the lock*. So every test here leaves the lock FREE, which is
both the common case and the broken one -- the serialization was never what
made it unsafe.

The callbacks register on the consumer's own ``add_message_received``: that
is the event #238 is about, and it is dispatched on the consumer thread, so
the thread identity under test is the real one and not a stand-in. The
consumer routes a handler's exception to its error sink rather than
re-raising, so each callback records what it saw for the test to assert on.
"""

import importlib.util
import io
import os
import threading
import time

import pytest

from daqifi.sdcard import END_OF_FILE_MARKER

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes_238",
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


def _wait_for(predicate, timeout=5.0, what="condition"):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.005)


class _CallbackOnce:
    """Runs ``action()`` on the consumer thread the first time a message
    arrives, recording the outcome; counts every message after that."""

    def __init__(self, action):
        self._action = action
        self.fired = threading.Event()
        self.outcome = None          # ("returned", value) | ("raised", exc)
        self.thread_name = None
        self.later_messages = 0

    def __call__(self, args):
        if self.fired.is_set():
            self.later_messages += 1
            return
        self.thread_name = threading.current_thread().name
        try:
            self.outcome = ("returned", self._action())
        except BaseException as exc:                              # noqa: BLE001
            # BROAD on purpose: this records a verdict, so anything that
            # escapes must be visible rather than silenced.
            self.outcome = ("raised", exc)
        finally:
            self.fired.set()


def _device_with_callback(action, response_map=None):
    device = _fakes._make_device_with_serial_facade(response_map)
    callback = _CallbackOnce(action)
    device._consumer.add_message_received(callback)
    return device, callback


def _run_callback_and_get_outcome(device, callback):
    """Deliver one frame, wait for the callback, and return its outcome."""
    stream = device._transport.stream
    assert device._consumer.is_running, "consumer should be running to start with"
    # The lock must be FREE: main's existing refusal only covers contention,
    # and this is the case it lets through.
    assert device._text_exchange_lock.acquire(blocking=False), (
        "the exchange lock must be free for this test to exercise the "
        "uncontended case"
    )
    device._text_exchange_lock.release()
    stream.feed(_frame())
    _wait_for(callback.fired.is_set, 10.0, "the callback to run")
    return callback.outcome


# ---- #238: the exchange itself, from the consumer thread -------------------


@pytest.mark.parametrize(
    "name, action",
    [
        # execute_text_command's path: on main this returns NORMALLY and
        # leaves the consumer dead -- a silent kill, not an error.
        ("drain_errors", lambda d: d.drain_errors(
            max_iterations=2, response_timeout_ms=100, completion_timeout_ms=50)),
        # the raw-capture path, reached through @_exclusive_sd_operation.
        ("get_sd_card_files", lambda d: d.get_sd_card_files()),
        ("request_sd_card_file_list", lambda d: d.request_sd_card_file_list()),
        ("delete_sd_card_file", lambda d: d.delete_sd_card_file("log_a.bin")),
        ("format_sd_card", lambda d: d.format_sd_card()),
    ],
)
def test_a_text_exchange_from_a_callback_is_refused_and_the_consumer_lives(
    name, action
) -> None:
    device, callback = _device_with_callback(
        lambda: action(device), {b"SD:LIST?": b"log_a.bin\r\n"}
    )
    try:
        kind, payload = _run_callback_and_get_outcome(device, callback)

        # 1. It is refused, and the error says where to call it from.
        assert kind == "raised", (
            f"{name} from a message-received callback returned "
            f"{payload!r} instead of refusing; on main this is the silent "
            f"case that kills the consumer"
        )
        assert isinstance(payload, RuntimeError), f"{name} raised {payload!r}"
        message = str(payload)
        assert "consumer" in message and "another thread" in message, (
            f"{name}'s refusal must say what is wrong and what to do "
            f"instead; got {message!r}"
        )

        # 2. The consumer is still running -- the point of #238. On main the
        #    pause already set _is_running False before anything refused.
        assert device._consumer.is_running, (
            f"{name} from a callback stopped the consumer"
        )

        # 3. And it still delivers: a later frame reaches the handler. The
        #    flag alone could be true while the thread had exited.
        device._transport.stream.feed(_frame(3))
        _wait_for(
            lambda: callback.later_messages > 0, 10.0,
            "a message after the refusal",
        )
        assert device._consumer._thread is not None
        assert device._consumer._thread.is_alive()
    finally:
        device.disconnect()


@pytest.mark.parametrize(
    "name, action",
    [
        ("drain_errors", lambda d: d.drain_errors(
            max_iterations=2, response_timeout_ms=100, completion_timeout_ms=50)),
        ("get_sd_card_files", lambda d: d.get_sd_card_files()),
    ],
)
def test_a_callbacks_text_exchange_leaves_the_pipeline_reading(name, action) -> None:
    """The HARM, on its own channel.

    Separate from the refusal test above on purpose: "it is refused" and
    "the consumer survives" are two claims, and a single test that asserts
    the first can never reach the second, so only one of them would have a
    failing run on main to stand on. This test asserts nothing about the
    outcome of the call -- only that the stream still has a reader
    afterwards. On main it fails because the consumer thread has exited:
    the exchange's pause set ``_is_running`` False from inside the consumer
    thread, so the loop ends as soon as the callback returns.
    """
    device, callback = _device_with_callback(
        lambda: action(device), {b"SD:LIST?": b"log_a.bin\r\n"}
    )
    try:
        _run_callback_and_get_outcome(device, callback)
        device._transport.stream.feed(_frame(3))
        _wait_for(
            lambda: callback.later_messages > 0, 10.0,
            f"a message after a callback called {name}",
        )
    finally:
        device.disconnect()


def test_the_refusal_does_not_depend_on_the_lock_being_held() -> None:
    """The guard is about the thread, not about contention.

    Named separately because it is the whole shape of the bug: main's
    refusal was `acquire(blocking=False)`, so a callback that happened to
    find the lock free proceeded into the broken path.
    """
    device, callback = _device_with_callback(
        lambda: device.drain_errors(
            max_iterations=2, response_timeout_ms=100, completion_timeout_ms=50
        )
    )
    try:
        # Nobody else is in an exchange, and nothing acquires the lock on
        # this path any more, so it is still free after the refusal.
        kind, payload = _run_callback_and_get_outcome(device, callback)
        assert kind == "raised" and isinstance(payload, RuntimeError)
        assert device._text_exchange_lock.acquire(blocking=False), (
            "the refusal must not leave the exchange lock held"
        )
        device._text_exchange_lock.release()
    finally:
        device.disconnect()


def test_an_exchange_from_an_ordinary_thread_still_works() -> None:
    """CONTROL -- passes on main too, by design.

    It proves the refusal is scoped to the consumer thread and has not
    swallowed the ordinary path; it is not evidence for the fix.
    """
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"log_a.bin\r\n"})
    try:
        assert [f.file_name for f in device.get_sd_card_files()] == ["log_a.bin"]
        assert device._consumer.is_running
    finally:
        device.disconnect()


# ---- #225: send() serialization -------------------------------------------


def test_a_send_from_another_thread_lands_after_an_exchange() -> None:
    """Thread B's send() waits for thread A's exchange instead of landing
    in the middle of it, where the exchange would read B's reply as its
    own. Fails on main, where send() takes no lock."""
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    try:
        in_exchange = threading.Event()
        release = threading.Event()

        def setup_action():
            in_exchange.set()
            # Hold the exchange open while thread B tries to send.
            release.wait(5.0)

        a = threading.Thread(
            target=lambda: device.execute_text_command(
                setup_action, response_timeout_ms=100, completion_timeout_ms=50
            ),
            name="exchange-A",
        )
        a.start()
        _wait_for(in_exchange.is_set, 5.0, "the exchange to start")

        from daqifi._internal import scpi_commands

        marker = scpi_commands.stop_streaming()
        b = threading.Thread(
            target=lambda: device.send(marker), name="sender-B"
        )
        b.start()
        # B is blocked on the lock: its bytes are not on the wire.
        time.sleep(0.3)
        sent_during = [w for w in list(stream.written) if b"STReam:STOP" in w]
        assert sent_during == [], (
            "send() landed in the middle of another thread's exchange"
        )

        release.set()
        a.join(10)
        b.join(10)
        assert not a.is_alive() and not b.is_alive()
        assert [w for w in stream.written if b"STReam:STOP" in w] != [], (
            "the send should have gone out once the exchange ended"
        )
    finally:
        release.set()
        device.disconnect()


def test_a_send_from_a_callback_does_not_wait_on_the_exchange_lock() -> None:
    """CONTROL -- passes on main too, where send() takes no lock at all.

    stop_streaming() from a message-received callback must not deadlock. A
    bare send neither pauses nor replaces the consumer, so unlike a text
    exchange it is allowed on that thread -- and it must not wait for a lock
    whose holder may be waiting for this very thread to stop the consumer.
    This guards the exemption that makes the new serialization safe, which
    is why it is here even though it cannot discriminate.
    """
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    try:
        # Hold the exchange lock from another thread for the whole callback,
        # so a send that waited for it would hang the consumer thread.
        held = threading.Event()
        release = threading.Event()

        def holder():
            # Hold it the way production does -- through _exchange_lock --
            # not with a raw acquire/release. The backlog of parked sends is
            # drained by whoever RELEASES the lock, so a raw release would
            # strand it and the test would be measuring its own scaffolding.
            with device._exchange_lock("test-holder"):
                held.set()
                release.wait(10.0)

        h = threading.Thread(target=holder, name="lock-holder")
        h.start()
        _wait_for(held.is_set, 5.0, "the lock to be held")

        callback = _CallbackOnce(lambda: device.stop_streaming())
        device._consumer.add_message_received(callback)
        stream.feed(_frame())
        # The callback must finish well inside the holder's window.
        assert callback.fired.wait(5.0), (
            "a send from a callback blocked on the exchange lock"
        )
        kind, _payload = callback.outcome
        assert kind == "returned", f"stop_streaming from a callback {callback.outcome!r}"
        # ⚠ CONTRACT CHANGED, DELIBERATELY. It used to go straight out from
        # here, because send() exempted the consumer thread from the lock.
        # That exemption let a callback's command land INSIDE a
        # run_exclusive group; it is gone, and the command is parked
        # instead. Not blocking is still the thing under test, and is still
        # asserted above -- what changed is that the command now lands
        # after the holder releases rather than during. See
        # test_a_callbacks_send_cannot_interleave_inside_run_exclusive.
        assert [w for w in stream.written if b"STReam:STOP" in w] == [], (
            "the callback's send bypassed the queue while the lock was held"
        )
        assert device._consumer.is_running
        release.set()
        h.join(5)
        _wait_for(
            lambda: [w for w in stream.written if b"STReam:STOP" in w] != [],
            10.0, "the parked send to be replayed once the lock is free",
        )
    finally:
        release.set()
        h.join(5)
        device.disconnect()


@pytest.mark.parametrize(
    "name, nested",
    [
        ("execute_text_command", lambda d: d.execute_text_command(
            lambda: None, response_timeout_ms=50, completion_timeout_ms=20)),
        ("get_sd_card_files", lambda d: d.get_sd_card_files()),
    ],
)
def test_an_exchange_from_a_status_handler_during_a_send_does_not_hang(
    name, nested
) -> None:
    """Serializing send() makes the sending thread a lock holder, and a
    failing send notifies subscribers of Lost. An exchange started from
    such a handler validates only AFTER acquiring the lock, so it hangs if
    the handler runs while the lock is held -- and because
    _deliver_status_event swallows handler exceptions, it presents as a
    silent hang rather than an error.

    The fix is that no user callback runs under the lock at all: the event
    is queued and delivered after the release, so by the time the handler
    runs the lock is free and these simply work. (An earlier revision
    instead recorded send() as the lock owner, which converted them into
    "not re-entrant" refusals -- better than hanging, but it only ever
    covered the SAME thread. See
    test_a_status_handler_may_wait_on_another_thread_that_sends for the
    case that defeated it.)

    A nested send() is deliberately not in this list: the device reads
    disconnected by the time the handler runs, so it raises
    DeviceNotConnectedError before reaching the lock. Asserting it here
    would be a test that passes either way.

    The sender runs on a worker with a join deadline so a regression is a
    FAILED test and not a run that never finishes.
    """
    from daqifi._internal import scpi_commands

    device = _fakes._make_device_with_serial_facade(
        {b"SD:LIST?": b"log_a.bin\r\n"}
    )
    stream = device._transport.stream
    try:
        reentered = []

        def on_status(_evt):
            try:
                nested(device)
                reentered.append("completed")
            except BaseException as exc:                          # noqa: BLE001
                reentered.append(f"{type(exc).__name__}: {exc}")

        device.add_status_changed(on_status)

        def failing_write(_data):
            raise IOError("write failed")

        stream.write = failing_write
        done = threading.Event()

        def sender():
            try:
                # A zero-argument command: the WRITE is what must fail, so
                # the command itself must not raise before reaching it.
                device.send(scpi_commands.stop_streaming())
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=sender, name="failing-sender", daemon=True).start()
        finished = done.wait(5.0)
    finally:
        # Do NOT disconnect a device whose exchange lock a wedged thread
        # still holds: teardown then blocks too and the regression becomes a
        # test run that never ends instead of a failure. The device is a
        # local fake on a daemon thread, so leaving it is harmless.
        if done.is_set():
            device.disconnect()
    assert finished, (
        f"a status handler's {name} deadlocked against the exchange lock "
        f"held by the send that failed"
    )
    assert reentered, "the status handler never ran"


# ---- #225: run_exclusive ---------------------------------------------------


def test_run_exclusive_keeps_two_threads_sequences_from_interleaving() -> None:
    device = _fakes._make_device_with_serial_facade()
    try:
        order = []
        order_lock = threading.Lock()

        def sequence(tag):
            def body():
                for step in range(3):
                    with order_lock:
                        order.append(f"{tag}{step}")
                    time.sleep(0.02)
            device.run_exclusive(body)

        threads = [
            threading.Thread(target=sequence, args=(tag,), name=f"seq-{tag}")
            for tag in "AB"
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert all(not t.is_alive() for t in threads)
        # Each sequence appears as an unbroken run of three.
        assert order in (
            ["A0", "A1", "A2", "B0", "B1", "B2"],
            ["B0", "B1", "B2", "A0", "A1", "A2"],
        ), order
    finally:
        device.disconnect()


def test_run_exclusive_is_re_entrant_for_the_thread_already_in_an_exchange() -> None:
    device = _fakes._make_device_with_serial_facade()
    try:
        def outer():
            return device.run_exclusive(lambda: "inner ran")

        assert device.run_exclusive(outer) == "inner ran"
    finally:
        device.disconnect()


def test_run_exclusive_is_refused_on_the_consumer_thread() -> None:
    device, callback = _device_with_callback(
        lambda: device.run_exclusive(lambda: "should not run")
    )
    try:
        kind, payload = _run_callback_and_get_outcome(device, callback)
        assert kind == "raised" and isinstance(payload, RuntimeError), payload
        assert device._consumer.is_running
    finally:
        device.disconnect()


def test_an_sd_operation_inside_a_text_capture_is_still_refused() -> None:
    """The refusal that must survive, stated against the right condition.

    An SD operation started from inside a text CAPTURE would switch the SPI
    bus out from under the reader currently on the stream (#164 item 6).
    The condition is the capture, NOT merely holding the exchange lock: an
    earlier revision of this branch keyed it on ownership, which also
    refused ``run_exclusive(lambda: get_sd_card_files())`` -- grouping an
    SD listing with other commands, which is what run_exclusive is for.
    That is covered by test_run_exclusive_can_group_an_sd_operation.
    """
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"log_a.bin\r\n"})
    refusals = []
    try:
        def setup():
            try:
                device.get_sd_card_files()
            except RuntimeError as exc:
                refusals.append(str(exc))

        device.execute_text_command(
            setup, response_timeout_ms=100, completion_timeout_ms=50
        )
    finally:
        device.disconnect()
    assert refusals and "inside another exchange" in refusals[0], refusals


# ---- audit r1: three defects this branch introduced or left --------------


def test_run_exclusive_can_group_a_text_query() -> None:
    """r1 #1: run_exclusive exists to group commands, and a grouped
    sequence is normally configure-then-QUERY. Holding the lock made this
    thread the owner, and only SD operations were permitted a nested
    exchange, so the query raised "not re-entrant" AFTER the configuration
    had already been applied -- the worst place to fail.

    Owning the exchange is now the single permission to nest one.
    """
    device = _fakes._make_device_with_serial_facade()
    try:
        sent = []

        def configure_then_query():
            device.send(_scpi().set_protobuf_stream_format())
            sent.append("configured")
            return device.drain_errors(
                max_iterations=2, response_timeout_ms=100,
                completion_timeout_ms=50,
            )

        errors = device.run_exclusive(configure_then_query)
        assert sent == ["configured"]
        assert isinstance(errors, list)
    finally:
        device.disconnect()


def test_run_exclusive_can_group_an_sd_operation() -> None:
    """r1 #1, same rule from the other side: grouping an SD listing with
    other commands is exactly what run_exclusive is for."""
    device = _fakes._make_device_with_serial_facade(
        {b"SD:LIST?": b"log_a.bin\r\n"}
    )
    try:
        files = device.run_exclusive(lambda: device.get_sd_card_files())
        assert [f.file_name for f in files] == ["log_a.bin"]
    finally:
        device.disconnect()


def test_a_status_handler_may_wait_on_another_thread_that_sends() -> None:
    """r1 #2: the deadlock my owner-recording did NOT fix.

    A failed send delivered the Lost event synchronously while still
    holding the exchange lock. A handler that hands recovery to another
    thread and waits for it deadlocks: the worker's send needs the lock the
    original sender still holds, and the sender cannot release it until the
    handler returns. Recording the owner thread cannot help -- the waiting
    is done by a DIFFERENT thread.

    So no user callback may run while this thread holds the lock.
    """
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    try:
        worker_finished = []

        def on_status(_evt):
            # ⚠ The worker must do something that actually REACHES the
            # lock, and most entry points do not once the device is Lost:
            # send() and drain_errors() both raise on `is_connected`
            # BEFORE acquiring, so a worker using either finishes even with
            # the defect present and the test discriminates nothing. Both
            # were tried here and both passed on the defective head.
            # run_exclusive acquires first and validates after, which is
            # also what a real recovery path does (reconnect, then work).
            def worker():
                try:
                    device.run_exclusive(lambda: None)
                except BaseException:                             # noqa: BLE001
                    pass

            w = threading.Thread(target=worker, name="recovery", daemon=True)
            w.start()
            w.join(3.0)
            worker_finished.append(not w.is_alive())

        device.add_status_changed(on_status)
        stream.write = lambda _d: (_ for _ in ()).throw(IOError("write failed"))

        done = threading.Event()

        def sender():
            try:
                device.send(_scpi().stop_streaming())
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=sender, name="failing-sender", daemon=True).start()
        finished = done.wait(15.0)
    finally:
        if done.is_set():
            device.disconnect()
    assert finished, "the failing send never returned"
    # The handler may run more than once: two sends can fail and each emits
    # its own Lost event. That is pre-existing behaviour (base does it too)
    # and not what this test is about, so assert on EVERY invocation rather
    # than on how many there were.
    assert worker_finished, "the status handler never ran"
    assert all(worker_finished), (
        "a status handler waited on a thread whose exchange needed the lock "
        f"the handler's own sender still held: {worker_finished}"
    )


def test_a_send_queued_behind_the_lock_rechecks_the_connection() -> None:
    """r1 #3: send() validated the connection before queueing and never
    again. Thread A's write fails and marks the device Lost; thread B,
    already queued on the lock, then wrote its command anyway, leaving the
    device Lost AND Streaming."""
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    try:
        gate = threading.Event()
        in_write = threading.Event()
        calls = []

        def write(data):
            calls.append(bytes(data))
            if len(calls) == 1:
                in_write.set()
                gate.wait(5.0)
                raise IOError("write timed out")
            return len(data)

        stream.write = write

        a_done = threading.Event()

        def thread_a():
            try:
                device.send(_scpi().stop_streaming())
            except Exception:
                pass
            finally:
                a_done.set()

        threading.Thread(target=thread_a, name="A", daemon=True).start()
        assert in_write.wait(5.0), "A never reached the write"

        b_outcome = []

        def thread_b():
            try:
                device.send(_scpi().start_streaming(1000))
                b_outcome.append("sent")
            except BaseException as exc:                          # noqa: BLE001
                b_outcome.append(type(exc).__name__)

        b = threading.Thread(target=thread_b, name="B", daemon=True)
        b.start()
        time.sleep(0.3)          # B is now queued on the exchange lock
        gate.set()               # A's write fails -> device goes Lost
        assert a_done.wait(10.0)
        b.join(10.0)

        # ⚠ CONTRACT CHANGED, DELIBERATELY, and the HARM assertion below is
        # unchanged. B no longer waits on the lock, so it cannot be told by
        # an exception that the link died while it waited: it parks, returns,
        # and the drop is recorded instead. The defect this test exists for
        # -- a queued command reaching a dead link -- is still asserted
        # exactly as before, and still fails without the fix.
        assert b_outcome == ["sent"], (
            f"B's send should have parked and returned: {b_outcome}"
        )
        assert not [c for c in calls if b"STReam:START" in c], (
            "a START reached the wire after the device went Lost"
        )
        assert device.delivery_fault is not None, (
            "the parked command was dropped with no trace, so the caller "
            "has no way to learn its command never went out"
        )
        assert "dropped" in device.delivery_fault
    finally:
        device.disconnect()


# ---- audit r2: the raw-capture path did not set the capture marker -------


_DL_BODY = b"y" * 1024


def _download_device():
    """A device whose SD:GET answers a 1024-byte file in 256-byte reads."""
    return _fakes._make_device_with_serial_facade(
        {
            b"SD:GET": b"DAQIFI> " + _DL_BODY + END_OF_FILE_MARKER,
            b"SD:LIST?": b"log_a.bin\r\n",
        },
        max_read_bytes=256,
    )


@pytest.mark.parametrize(
    "name, nested",
    [
        ("get_sd_card_files", lambda d, _s: d.get_sd_card_files()),
        ("download_sd_card_file",
         lambda d, _s: d.download_sd_card_file("log_b.bin", io.BytesIO())),
        ("execute_text_command", lambda d, _s: d.execute_text_command(
            lambda: None, response_timeout_ms=50, completion_timeout_ms=20)),
    ],
)
def test_an_sd_operation_from_a_download_progress_callback_is_refused(
    name, nested
) -> None:
    """r2 #1: a RAW capture never marked itself as a capture in progress.

    The r1 audit moved the nesting permission off lock ownership and onto
    "a capture is in progress on this thread", which was the right
    condition -- but only the TEXT capture set the marker. A raw SD
    download therefore looked like no capture at all, so its own progress
    callback could start a listing or a second download, and that inner
    operation read the outer download's remaining bytes and its EOF marker
    off the same stream: the inner one "succeeded" with the outer file's
    data and the outer one timed out short.

    Base refused these (it keyed on ownership, which a download does set),
    so this is a guard the r1 fix REMOVED -- a narrower gate than the
    action it guards. The marker is now set by one helper that every
    capture path goes through, text and raw alike.
    """
    device = _download_device()
    sink = io.BytesIO()
    refusals = []

    def on_progress(_p):
        if refusals:
            return
        try:
            nested(device, sink)
            refusals.append("COMPLETED -- not refused")
        except RuntimeError as exc:
            refusals.append(str(exc))

    try:
        result = device.download_sd_card_file(
            "log_a.bin", sink, progress=on_progress
        )
    finally:
        device.disconnect()

    assert refusals, f"the progress callback never ran, so {name} was untested"
    assert "COMPLETED" not in refusals[0], (
        f"{name} ran inside a raw download's progress callback and consumed "
        f"its stream: {refusals[0]}"
    )
    # The outer download must still arrive whole: the point of the refusal.
    assert sink.getvalue() == _DL_BODY, (
        f"the outer download lost bytes to a nested {name}: got "
        f"{len(sink.getvalue())} of {len(_DL_BODY)}"
    )
    assert result.file_size == len(_DL_BODY)


# ---- audit r3: the refusal must precede the first write -----------------


def _run_exclusive_inside_a_capture():
    """Run run_exclusive(grouped) from inside a setup_action.

    Returns (refusals, written) so the two claims -- "it is refused
    clearly" and "nothing reached the wire" -- can be asserted in separate
    tests. One test cannot carry both: whichever assertion comes first is
    the only one with a failing run behind it, and on the defective head
    the message differs AND the write happens, so an ordering choice would
    silently decide which defect was evidenced.
    """
    device = _fakes._make_device_with_serial_facade()
    stream = device._transport.stream
    refusals = []

    def grouped():
        # A configure-then-query group: the configure is the side effect
        # that must not happen.
        device.send(_scpi().start_streaming(1000))
        return device.drain_errors(
            max_iterations=1, response_timeout_ms=50, completion_timeout_ms=20
        )

    def setup():
        try:
            device.run_exclusive(grouped)
            refusals.append("COMPLETED -- not refused")
        except RuntimeError as exc:
            refusals.append(str(exc))

    try:
        device.execute_text_command(
            setup, response_timeout_ms=100, completion_timeout_ms=50
        )
    finally:
        device.disconnect()
    assert refusals, "the setup_action never ran, so nothing was tested"
    return refusals, list(stream.written)


def test_run_exclusive_inside_a_text_capture_writes_nothing() -> None:
    """r3, the HARM: a fail-closed check placed after its own side effect
    fails open.

    run_exclusive's ownership fast path ran ``fn`` without checking the
    capture marker. Inside a ``setup_action`` the thread already owns the
    lock, so the fast path was taken and the grouped sequence RAN: its
    START reached the device, and only a later capture inside the group
    raised. A refusal that arrives after the device has changed state is
    not a refusal.

    This asserts on the WIRE and nothing else.
    """
    _refusals, written = _run_exclusive_inside_a_capture()
    started = [w for w in written if b"STReam:START" in w]
    assert started == [], (
        "the grouped sequence's START reached the device before the "
        f"refusal: {started}"
    )


def test_run_exclusive_inside_a_text_capture_says_why() -> None:
    """r3, the CONTRACT: the changelog says run_exclusive is refused inside
    a text capture, so the error must come from that rule and name it --
    not from whatever the grouped sequence happened to do next."""
    refusals, _written = _run_exclusive_inside_a_capture()
    assert "COMPLETED" not in refusals[0], (
        f"run_exclusive ran inside a text capture: {refusals[0]}"
    )
    assert "run_exclusive cannot run inside another exchange" in refusals[0], (
        refusals[0]
    )
