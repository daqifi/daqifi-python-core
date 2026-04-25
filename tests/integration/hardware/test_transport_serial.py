"""B1 — SerialStreamTransport hardware tests.

Drives the low-level :class:`daqifi.transport.SerialStreamTransport`
directly against the real serial port (no NyquistDevice / no protobuf
consumer) so the transport-layer contract can be verified independently
of every layer above it.

Each test that opens the port does so on its own short-lived transport
instance and disconnects in a finally block; the session-scoped
``device_info`` fixture resolves the port name once and is shared.
"""
from __future__ import annotations

import sys
import time
from typing import List

import pytest
import serial

from daqifi._internal.comm import (
    ConnectionRetryOptions,
    TransportStatusEventArgs,
)
from daqifi.discovery import DeviceInfo
from daqifi.transport import SerialStreamTransport


# ---- Helpers ----------------------------------------------------------------


def _new_transport(port: str, **overrides) -> SerialStreamTransport:
    """Construct a transport with the project's normal streaming defaults."""
    kwargs = {
        "port_name": port,
        "baud_rate": 115200,
        "os_rx_buffer_bytes": 65536,
        "os_tx_buffer_bytes": 65536,
    }
    kwargs.update(overrides)
    return SerialStreamTransport(**kwargs)


# ---- __init__ validation (no hardware) --------------------------------------


def test_init_rejects_empty_port_name() -> None:
    with pytest.raises(ValueError, match="port_name"):
        SerialStreamTransport(port_name="")


@pytest.mark.parametrize("kw", ["os_rx_buffer_bytes", "os_tx_buffer_bytes"])
def test_init_rejects_non_positive_buffer_size(kw: str) -> None:
    with pytest.raises(ValueError, match=kw):
        SerialStreamTransport(port_name="COM_FAKE", **{kw: 0})


# ---- get_available_port_names -----------------------------------------------


def test_get_available_port_names_includes_real_port(
    device_info: DeviceInfo,
) -> None:
    """The discovered port must show up in the static enumeration.

    DeviceFinder may return a port name with trailing whitespace
    (Windows registry quirk); normalize before the membership check.
    """
    ports = SerialStreamTransport.get_available_port_names()
    assert isinstance(ports, list)
    discovered = device_info.port.strip()
    normalized = [p.strip() for p in ports]
    assert discovered in normalized, (
        f"discovered port {discovered!r} not in enumeration {normalized!r}"
    )


# ---- connect / disconnect happy path ----------------------------------------


def test_connect_then_disconnect_round_trip(device_info: DeviceInfo) -> None:
    t = _new_transport(device_info.port)
    assert t.is_connected is False
    assert "Disconnected" in t.connection_info
    try:
        t.connect()
        assert t.is_connected is True
        # connection_info reports port + baud once open.
        info = t.connection_info
        assert device_info.port in info
        assert "115200" in info
        # Underlying serial.Serial exposed via .stream and ready for I/O.
        s = t.stream
        assert s is not None
        assert isinstance(s, serial.Serial)
        assert s.is_open is True
        # Post-open read timeout should drop to 500ms (matches upstream
        # behaviour for prompt consumer-thread shutdown).
        assert s.timeout == pytest.approx(0.5)
    finally:
        t.close()
    assert t.is_connected is False
    assert "Disconnected" in t.connection_info


def test_double_connect_is_noop(device_info: DeviceInfo) -> None:
    """Second connect on an already-open transport must be a silent no-op,
    not a re-open (which would briefly bounce DTR/RTS on the device)."""
    t = _new_transport(device_info.port)
    try:
        t.connect()
        first_handle = t.stream
        assert first_handle is not None
        t.connect()  # second call
        assert t.is_connected is True
        # Same underlying handle — no re-open happened.
        assert t.stream is first_handle
    finally:
        t.close()


def test_disconnect_is_idempotent(device_info: DeviceInfo) -> None:
    t = _new_transport(device_info.port)
    t.connect()
    t.disconnect()
    assert t.is_connected is False
    # Calling disconnect again is allowed and must not raise.
    t.disconnect()
    assert t.is_connected is False


def test_close_is_idempotent_and_marks_disposed(
    device_info: DeviceInfo,
) -> None:
    t = _new_transport(device_info.port)
    t.connect()
    t.close()
    t.close()  # second close is a no-op
    # Post-close the transport is disposed; .stream raises.
    with pytest.raises(RuntimeError, match="closed"):
        _ = t.stream
    # connect() on a disposed transport also raises.
    with pytest.raises(RuntimeError, match="closed"):
        t.connect()


# ---- DTR / RTS toggling -----------------------------------------------------


@pytest.mark.parametrize(
    "enable_dtr,enable_rts",
    [(True, False), (False, False), (True, True), (False, True)],
)
def test_dtr_rts_settings_applied(
    device_info: DeviceInfo, enable_dtr: bool, enable_rts: bool
) -> None:
    """DTR/RTS constructor flags must propagate to the underlying serial.Serial."""
    t = _new_transport(
        device_info.port, enable_dtr=enable_dtr, enable_rts=enable_rts
    )
    try:
        t.connect()
        s = t.stream
        assert s is not None
        # pyserial reflects the line state on the .dtr/.rts attributes;
        # this validates the attribute landed on the underlying handle
        # (the actual electrical line state is hardware-dependent and
        # not always queryable on USB CDC).
        assert s.dtr is enable_dtr
        assert s.rts is enable_rts
    finally:
        t.close()


# ---- Status-changed callbacks ----------------------------------------------


def test_status_callback_fires_on_connect_and_disconnect(
    device_info: DeviceInfo,
) -> None:
    events: List[TransportStatusEventArgs] = []

    def handler(ev: TransportStatusEventArgs) -> None:
        events.append(ev)

    t = _new_transport(device_info.port)
    t.add_status_changed(handler)
    try:
        t.connect()
    finally:
        # close() invokes disconnect() once; rely on that single
        # teardown path so we don't observe the documented
        # "disconnect() always fires once per call" behaviour twice.
        t.close()

    statuses = [(e.is_connected, e.error) for e in events]
    assert statuses == [(True, None), (False, None)], (
        f"unexpected status sequence: {statuses!r}"
    )
    # connection_info on the connect event includes the live port + baud.
    assert device_info.port.strip() in events[0].connection_info
    assert "115200" in events[0].connection_info


def test_remove_status_changed_stops_notifications(
    device_info: DeviceInfo,
) -> None:
    events: List[TransportStatusEventArgs] = []

    def handler(ev: TransportStatusEventArgs) -> None:
        events.append(ev)

    t = _new_transport(device_info.port)
    t.add_status_changed(handler)
    t.remove_status_changed(handler)
    # Removing again is silent (matches .NET event-handler semantics).
    t.remove_status_changed(handler)
    try:
        t.connect()
        t.disconnect()
    finally:
        t.close()
    assert events == []


def test_status_callback_exception_does_not_break_other_callbacks(
    device_info: DeviceInfo,
) -> None:
    """A misbehaving callback must not poison subsequent ones (mirrors
    .NET ``EventHandler?.Invoke`` semantics)."""
    seen: List[bool] = []

    def bad(ev: TransportStatusEventArgs) -> None:
        raise RuntimeError("intentional callback failure")

    def good(ev: TransportStatusEventArgs) -> None:
        seen.append(ev.is_connected)

    t = _new_transport(device_info.port)
    t.add_status_changed(bad)
    t.add_status_changed(good)
    try:
        t.connect()
    finally:
        t.close()
    assert seen == [True, False]


# ---- Port-not-found error path ---------------------------------------------


def test_connect_to_missing_port_raises_and_fires_error_status() -> None:
    fake = "COM_DOES_NOT_EXIST_999" if sys.platform == "win32" else "/dev/ttyDOES_NOT_EXIST_999"
    events: List[TransportStatusEventArgs] = []
    t = SerialStreamTransport(port_name=fake)
    t.add_status_changed(events.append)
    try:
        with pytest.raises(serial.SerialException):
            t.connect()
    finally:
        t.close()
    # is_connected must remain False; at least one event must carry
    # the underlying SerialException (close() also fires a clean
    # (False, None) on the way out — accept either ordering).
    assert t.is_connected is False
    error_events = [
        e for e in events if not e.is_connected and e.error is not None
    ]
    assert error_events, (
        f"expected a (False, error) status event; got {events!r}"
    )
    assert isinstance(error_events[0].error, serial.SerialException)


def test_connect_no_retry_fails_fast_on_missing_port() -> None:
    """A no-retry policy on a missing port should raise on the first
    attempt without burning seconds on retry-loop delays."""
    fake = "COM_DOES_NOT_EXIST_999" if sys.platform == "win32" else "/dev/ttyDOES_NOT_EXIST_999"
    t = SerialStreamTransport(port_name=fake)
    started = time.monotonic()
    try:
        with pytest.raises(serial.SerialException):
            t.connect(retry_options=ConnectionRetryOptions.no_retry())
    finally:
        t.close()
    # Single attempt — should be well under 1 second even with timeouts.
    assert (time.monotonic() - started) < 2.0


# ---- Retry options (happy path with retries enabled) -----------------------


def test_connect_with_retry_options_succeeds(device_info: DeviceInfo) -> None:
    """Passing a `fast` retry policy to a real port must still succeed
    on the first attempt; this exercises the retry-loop branch even
    though the loop only runs once."""
    t = _new_transport(device_info.port)
    try:
        t.connect(retry_options=ConnectionRetryOptions.fast())
        assert t.is_connected is True
    finally:
        t.close()


# ---- set_buffer_size reflection --------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="set_buffer_size is a Win32 SetupComm wrapper; no-op on Linux/macOS.",
)
def test_set_buffer_size_called_on_windows(device_info: DeviceInfo) -> None:
    """On Windows, the constructor's os_rx/tx_buffer_bytes must be passed
    to the underlying pyserial set_buffer_size call. We can't directly
    read back the OS buffer size, but we can verify the method exists
    and didn't raise (which would have aborted the open)."""
    t = _new_transport(
        device_info.port, os_rx_buffer_bytes=8192, os_tx_buffer_bytes=8192
    )
    try:
        t.connect()
        s = t.stream
        assert s is not None
        # pyserial-on-Windows exposes set_buffer_size.
        assert callable(getattr(s, "set_buffer_size", None))
        # If we got here, the open path's set_buffer_size invocation
        # succeeded (or was caught and debug-logged). The transport is
        # connected — that's the visible contract.
        assert t.is_connected is True
    finally:
        t.close()
