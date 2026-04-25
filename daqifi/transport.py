"""Concrete transport implementations: TCP, UDP, Serial, HID.

Ports the four concrete transports from
``Daqifi.Core/Communication/Transport/`` (TcpStreamTransport,
UdpTransport, SerialStreamTransport, HidLibraryTransport @ upstream
e8d84af). All are sync-only per the project convention -- the upstream
async/sync overload pairs collapse to a single sync API. Each transport
satisfies the corresponding Protocol from
:mod:`daqifi._internal.comm` (StreamTransport / UdpTransport /
HidTransport).

Status notifications use the project's ``add_<event>`` /
``remove_<event>`` callback list pattern (mirrors :class:`StreamTransport`
in ``_internal/comm.py``).
"""

from __future__ import annotations

import io
import socket
import threading
import time
from datetime import timedelta
from typing import IO, Callable, List, Optional, Tuple

import serial

from ._internal.comm import (
    ConnectionRetryOptions,
    HidDeviceInfo,
    TransportStatusEventArgs,
    UdpEndPoint,
)
from ._internal.hid_enumerator import HidApiPlatform, HidPlatform


__all__ = [
    "TcpStreamTransport",
    "SerialStreamTransport",
    "UdpTransport",
    "HidLibraryTransport",
]


_DEFAULT_TIMEOUT_SECONDS = 5.0


# ---- Helpers -----------------------------------------------------------------


def _notify(handlers: List[Callable[[TransportStatusEventArgs], None]],
            args: TransportStatusEventArgs) -> None:
    """Invoke each callback; swallow per-callback exceptions to mimic
    .NET's ``EventHandler?.Invoke`` semantics where one bad subscriber
    doesn't kill the rest."""
    for h in list(handlers):
        try:
            h(args)
        except Exception:
            pass


def _validate_positive_timeout(value: timedelta, name: str) -> None:
    if value.total_seconds() <= 0:
        raise ValueError(f"{name} must be greater than zero.")


def _validate_usb_identifier(value: int, name: str) -> None:
    if value < 0 or value > 0xFFFF:
        raise ValueError(f"{name} must be in the range 0..65535.")


# ---- TcpStreamTransport ------------------------------------------------------


class TcpStreamTransport:
    """TCP implementation of :class:`StreamTransport` Protocol.

    Ports the C# ``TcpStreamTransport``. Wraps a :class:`socket.socket`
    in a ``rwb`` file-like view (via :meth:`socket.makefile`) so it
    matches the ``IO[bytes]`` shape consumers expect.

    Like upstream, accepts either an explicit IP address string or a
    hostname; hostname resolution happens at :meth:`connect` time.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._socket: Optional[socket.socket] = None
        self._stream: Optional[IO[bytes]] = None
        self._disposed = False
        self._status_handlers: List[Callable[[TransportStatusEventArgs], None]] = []

    @property
    def stream(self) -> Optional[IO[bytes]]:
        self._raise_if_disposed()
        return self._stream

    @property
    def is_connected(self) -> bool:
        # Beyond the simple None-check, also verify the underlying
        # FD is still open and the stream wrapper hasn't been
        # closed — surfaces a closed-by-the-other-side socket as
        # "not connected" instead of letting the next send/recv
        # crash with a confusing OSError (Qodo PR 17 pass 24 #4).
        if self._socket is None or self._stream is None:
            return False
        if getattr(self._stream, "closed", False):
            return False
        try:
            return self._socket.fileno() != -1
        except Exception:
            return False

    @property
    def connection_info(self) -> str:
        if not self.is_connected:
            return f"TCP: Disconnected ({self._host}:{self._port})"
        try:
            local = self._socket.getsockname()  # type: ignore[union-attr]
        except Exception:
            local = "?"
        return f"TCP: {local} -> {self._host}:{self._port}"

    def add_status_changed(
        self, handler: Callable[[TransportStatusEventArgs], None]
    ) -> None:
        self._status_handlers.append(handler)

    def remove_status_changed(
        self, handler: Callable[[TransportStatusEventArgs], None]
    ) -> None:
        try:
            self._status_handlers.remove(handler)
        except ValueError:
            pass

    def connect(
        self, retry_options: Optional[ConnectionRetryOptions] = None
    ) -> None:
        self._raise_if_disposed()
        if self.is_connected:
            return

        options = retry_options or ConnectionRetryOptions.no_retry()
        max_attempts = options.max_attempts if options.enabled else 1
        last_exc: Optional[BaseException] = None

        for attempt in range(1, max_attempts + 1):
            # Track the locally-created socket separately from
            # self._socket so a failure between socket() and the final
            # self._socket = sock assignment doesn't leak the file
            # descriptor (Qodo PR 17 pass 2 finding 1).
            sock: Optional[socket.socket] = None
            try:
                if attempt > 1:
                    delay = options.calculate_delay(attempt)
                    if delay > 0:
                        time.sleep(delay)

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(options.connection_timeout)
                sock.connect((self._host, self._port))
                # Switch off the connect-timeout so blocking reads can
                # use socket.makefile's default semantics. We rely on
                # `disconnect()` calling `socket.shutdown(SHUT_RDWR)`
                # to interrupt any blocked `stream.read()` in the
                # consumer thread — that's the "wakeup" mechanism for
                # graceful stop without paying a per-iteration timeout.
                sock.settimeout(None)
                self._socket = sock
                # Hand-off complete; subsequent failure paths should
                # close via self._socket, not the local `sock`.
                sock = None
                self._stream = self._socket.makefile("rwb", buffering=0)
                self._notify_status(True, None)
                return
            except Exception as exc:
                last_exc = exc
                # Close whichever handle is alive: the local `sock`
                # (failure before hand-off) or self._socket (failure
                # after hand-off).
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
                if self._stream is not None:
                    try:
                        self._stream.close()
                    except Exception:
                        pass
                if self._socket is not None:
                    try:
                        self._socket.close()
                    except Exception:
                        pass
                self._socket = None
                self._stream = None

                if attempt < max_attempts and options.enabled:
                    self._notify_status(
                        False,
                        RuntimeError(
                            f"Connection attempt {attempt}/{max_attempts} "
                            f"failed, retrying..."
                        ),
                    )
                    continue

                self._notify_status(False, exc)
                raise

        # Defensive fallback (loop should always have raised or returned).
        self._notify_status(False, last_exc)
        raise RuntimeError("Connection failed after all retry attempts.")

    def disconnect(self) -> None:
        # Idempotent disconnect: don't early-return on
        # `not self.is_connected`. A peer-closed connection still
        # has a half-open socket that needs explicit close, and
        # callers / device-state observers depend on a definitive
        # disconnect event firing exactly once per disconnect()
        # call (Qodo PR 17 pass 37 #1).
        # Track whether the close path raised; status callbacks should
        # see the actual error rather than an immediately-following
        # `(False, None)` clean notification (Qodo PR 17 pass 4 #2).
        # Each close attempt is individually try/excepted so a failed
        # stream.close doesn't prevent socket.close from running and
        # leaking the file descriptor (Qodo PR 17 pass 10 #3).
        disconnect_error: Optional[BaseException] = None

        # Shutdown both halves of the socket BEFORE closing it.
        # This wakes any consumer thread blocked in `stream.read()`
        # (it observes EOF instead of hanging forever), letting
        # `StreamMessageConsumer.stop_safely()` actually stop the
        # daemon thread (Qodo PR 17 finding 5).
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                # Already closed / not connected — non-fatal.
                pass

        if self._stream is not None:
            try:
                self._stream.close()
            except Exception as exc:
                disconnect_error = disconnect_error or exc

        if self._socket is not None:
            try:
                self._socket.close()
            except Exception as exc:
                disconnect_error = disconnect_error or exc

        try:
            if disconnect_error is not None:
                raise disconnect_error
        finally:
            self._socket = None
            self._stream = None
            # Single notification with the captured error (or None on
            # clean disconnect). No double-fire on failure paths.
            self._notify_status(False, disconnect_error)

    def close(self) -> None:
        if self._disposed:
            return
        try:
            self.disconnect()
        except Exception:
            # Per upstream Dispose(): swallow disconnect errors.
            pass
        self._disposed = True

    def _notify_status(self, is_connected: bool, error: Optional[BaseException]) -> None:
        _notify(
            self._status_handlers,
            TransportStatusEventArgs(
                is_connected=is_connected,
                connection_info=self.connection_info,
                error=error,
            ),
        )

    def _raise_if_disposed(self) -> None:
        if self._disposed:
            raise RuntimeError("TcpStreamTransport has been closed.")


# ---- SerialStreamTransport ---------------------------------------------------


class SerialStreamTransport:
    """Serial-port implementation of :class:`StreamTransport`.

    Ports the C# ``SerialStreamTransport``. Backed by ``pyserial``
    (already a project dep -- see :class:`SerialInterface` in
    ``_internal/comm.py``). The connect-time read-timeout drops to
    500 ms after a successful open so consumer threads can stop
    promptly, mirroring upstream behaviour.
    """

    # OS-level USB CDC RX buffer hint for `set_buffer_size` (Windows
    # only — pyserial's call wraps Win32 SetupComm; other platforms
    # ignore it). 64 KiB is large enough to absorb a ~30 ms streaming
    # burst at 100 kHz × 2-byte samples without dropping, which is
    # well past the firmware's documented 1 ms read-cadence floor.
    DEFAULT_OS_RX_BUFFER_BYTES = 64 * 1024
    DEFAULT_OS_TX_BUFFER_BYTES = 64 * 1024

    def __init__(
        self,
        port_name: str,
        baud_rate: int = 9600,
        parity: str = serial.PARITY_NONE,
        data_bits: int = 8,
        stop_bits: float = serial.STOPBITS_ONE,
        enable_dtr: bool = True,
        enable_rts: bool = False,
        os_rx_buffer_bytes: int = DEFAULT_OS_RX_BUFFER_BYTES,
        os_tx_buffer_bytes: int = DEFAULT_OS_TX_BUFFER_BYTES,
    ) -> None:
        if not port_name:
            raise ValueError("port_name cannot be empty.")
        if os_rx_buffer_bytes <= 0:
            raise ValueError("os_rx_buffer_bytes must be positive.")
        if os_tx_buffer_bytes <= 0:
            raise ValueError("os_tx_buffer_bytes must be positive.")
        self._port_name = port_name
        self._baud_rate = baud_rate
        self._parity = parity
        self._data_bits = data_bits
        self._stop_bits = stop_bits
        self._enable_dtr = enable_dtr
        self._enable_rts = enable_rts
        self._os_rx_buffer_bytes = os_rx_buffer_bytes
        self._os_tx_buffer_bytes = os_tx_buffer_bytes
        self._serial: Optional[serial.Serial] = None
        self._disposed = False
        self._status_handlers: List[Callable[[TransportStatusEventArgs], None]] = []

    @property
    def stream(self) -> Optional[IO[bytes]]:
        self._raise_if_disposed()
        return self._serial  # type: ignore[return-value]

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def connection_info(self) -> str:
        if not self.is_connected:
            return f"Serial: Disconnected ({self._port_name})"
        return f"Serial: {self._port_name} @ {self._baud_rate} baud"

    def add_status_changed(
        self, handler: Callable[[TransportStatusEventArgs], None]
    ) -> None:
        self._status_handlers.append(handler)

    def remove_status_changed(
        self, handler: Callable[[TransportStatusEventArgs], None]
    ) -> None:
        try:
            self._status_handlers.remove(handler)
        except ValueError:
            pass

    def connect(
        self, retry_options: Optional[ConnectionRetryOptions] = None
    ) -> None:
        self._raise_if_disposed()
        if self.is_connected:
            return

        options = retry_options or ConnectionRetryOptions.no_retry()
        max_attempts = options.max_attempts if options.enabled else 1
        last_exc: Optional[BaseException] = None

        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    delay = options.calculate_delay(attempt)
                    if delay > 0:
                        time.sleep(delay)

                self._serial = serial.Serial(
                    port=self._port_name,
                    baudrate=self._baud_rate,
                    parity=self._parity,
                    bytesize=self._data_bits,
                    stopbits=self._stop_bits,
                    timeout=options.connection_timeout,
                    write_timeout=options.connection_timeout,
                    dsrdtr=False,
                )
                # pyserial 3.5 supports setting these after open().
                self._serial.dtr = self._enable_dtr
                self._serial.rts = self._enable_rts

                # Bump the OS-level USB CDC driver buffers (Windows
                # SetupComm, no-op on Linux/macOS where the kernel
                # owns the URB allocation). A larger driver buffer
                # absorbs streaming bursts that would otherwise drop
                # if the consumer thread takes a context-switch hit
                # mid-parse — firmware drops if the host doesn't read
                # within ~1 ms (per daqifi-python-test-suite/
                # test_harness.py:106 FastReader notes).
                set_buffer_size = getattr(self._serial, "set_buffer_size", None)
                if callable(set_buffer_size):
                    try:
                        set_buffer_size(
                            rx_size=self._os_rx_buffer_bytes,
                            tx_size=self._os_tx_buffer_bytes,
                        )
                    except (OSError, NotImplementedError):
                        # Linux + macOS pyserial don't implement this;
                        # silently fall back to the kernel default.
                        pass

                # Mirror upstream: drop ReadTimeout to 500ms post-open
                # so consumer threads can be stopped promptly.
                self._serial.timeout = 0.5

                self._notify_status(True, None)
                return
            except Exception as exc:
                last_exc = exc
                if self._serial is not None:
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                self._serial = None

                if attempt < max_attempts and options.enabled:
                    self._notify_status(
                        False,
                        RuntimeError(
                            f"Connection attempt {attempt}/{max_attempts} "
                            f"failed, retrying..."
                        ),
                    )
                    continue

                self._notify_status(False, exc)
                raise

        self._notify_status(False, last_exc)
        raise RuntimeError("Connection failed after all retry attempts.")

    def disconnect(self) -> None:
        # Idempotent disconnect: don't early-return on
        # `not is_connected`. Match the TcpStreamTransport behavior
        # so a half-open serial port still gets explicitly closed
        # and the (False, ...) status event fires exactly once per
        # disconnect() call (Qodo PR 17 pass 38 #1, mirroring pass
        # 37 #1).
        # Same single-notification pattern as TcpStreamTransport (Qodo
        # PR 17 pass 6 #2): capture the close error so subscribers see
        # the actual failure instead of a clean (False, None) that
        # immediately overwrites the (False, exc) from the except path.
        disconnect_error: Optional[BaseException] = None
        try:
            if self._serial is not None:
                self._serial.close()
        except Exception as exc:
            disconnect_error = exc
            raise
        finally:
            self._serial = None
            self._notify_status(False, disconnect_error)

    def close(self) -> None:
        if self._disposed:
            return
        try:
            self.disconnect()
        except Exception:
            pass
        self._disposed = True

    @staticmethod
    def get_available_port_names() -> List[str]:
        """Return available serial-port names (mirrors C# static helper).

        Uses :mod:`serial.tools.list_ports`, the canonical pyserial way
        to enumerate ports across Windows / Linux / macOS.
        """
        from serial.tools import list_ports

        return [p.device for p in list_ports.comports()]

    def _notify_status(self, is_connected: bool, error: Optional[BaseException]) -> None:
        _notify(
            self._status_handlers,
            TransportStatusEventArgs(
                is_connected=is_connected,
                connection_info=self.connection_info,
                error=error,
            ),
        )

    def _raise_if_disposed(self) -> None:
        if self._disposed:
            raise RuntimeError("SerialStreamTransport has been closed.")


# ---- UdpTransport ------------------------------------------------------------


class UdpTransport:
    """UDP broadcast/unicast implementation of :class:`UdpTransport` Protocol.

    Ports the C# ``UdpTransport``. Backed by a single ``SOCK_DGRAM``
    socket with broadcast enabled. ``local_port=0`` (default) lets the
    OS assign an ephemeral port; :attr:`local_port` exposes the bound
    value after :meth:`open`.
    """

    def __init__(self, local_port: int = 0) -> None:
        self._configured_port = local_port
        self._socket: Optional[socket.socket] = None
        self._disposed = False
        self._status_handlers: List[Callable[[TransportStatusEventArgs], None]] = []

    @property
    def is_open(self) -> bool:
        return self._socket is not None and not self._disposed

    @property
    def local_port(self) -> int:
        if self.is_open:
            try:
                return self._socket.getsockname()[1]  # type: ignore[union-attr]
            except Exception:
                pass
        return self._configured_port

    @property
    def connection_info(self) -> str:
        if not self.is_open:
            return f"UDP: Closed (LocalPort: {self._configured_port})"
        try:
            return f"UDP: Open ({self._socket.getsockname()})"  # type: ignore[union-attr]
        except Exception:
            return f"UDP: Open (LocalPort: {self._configured_port})"

    def add_status_changed(
        self, handler: Callable[[TransportStatusEventArgs], None]
    ) -> None:
        self._status_handlers.append(handler)

    def remove_status_changed(
        self, handler: Callable[[TransportStatusEventArgs], None]
    ) -> None:
        try:
            self._status_handlers.remove(handler)
        except ValueError:
            pass

    def open(self) -> None:
        self._raise_if_disposed()
        if self.is_open:
            return
        # Track local socket separately from self._socket so a failure
        # before the hand-off line doesn't leak the file descriptor
        # (Qodo PR 17 pass 4 — same pattern as TCP transport pass 2).
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except OSError:
                # Some platforms don't allow this; not fatal.
                pass
            sock.bind(("", self._configured_port))
            self._socket = sock
            sock = None
            self._notify_status(True, None)
        except Exception as exc:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            if self._socket is not None:
                try:
                    self._socket.close()
                except Exception:
                    pass
            self._socket = None
            self._notify_status(False, exc)
            raise

    def close(self) -> None:
        if not self.is_open:
            if not self._disposed:
                self._disposed = True
                # Fire a definitive (False, None) so observers see a
                # consistent "closed" event even when close() is
                # called on an already-closed transport (Qodo PR 17
                # pass 41 #2, mirroring the TCP/Serial idempotent
                # disconnect pattern).
                self._notify_status(False, None)
            return
        # Same single-notification pattern as TcpStreamTransport (Qodo
        # PR 17 pass 6 #3): one (False, error|None) call instead of two.
        close_error: Optional[BaseException] = None
        try:
            self._socket.close()  # type: ignore[union-attr]
        except Exception as exc:
            close_error = exc
            raise
        finally:
            self._socket = None
            self._disposed = True
            self._notify_status(False, close_error)

    def send_broadcast(self, data: bytes, port: int) -> None:
        self._raise_if_disposed()
        if not self.is_open:
            raise RuntimeError("UDP transport is not open.")
        self._socket.sendto(data, ("255.255.255.255", port))  # type: ignore[union-attr]

    def send_broadcast_to(self, data: bytes, end_point: UdpEndPoint) -> None:
        self._raise_if_disposed()
        if not self.is_open:
            raise RuntimeError("UDP transport is not open.")
        self._socket.sendto(data, end_point)  # type: ignore[union-attr]

    def send_unicast(self, data: bytes, end_point: UdpEndPoint) -> None:
        self._raise_if_disposed()
        if not self.is_open:
            raise RuntimeError("UDP transport is not open.")
        self._socket.sendto(data, end_point)  # type: ignore[union-attr]

    def receive(
        self, timeout: Optional[timedelta] = None
    ) -> Optional[Tuple[bytes, UdpEndPoint]]:
        self._raise_if_disposed()
        if not self.is_open:
            raise RuntimeError("UDP transport is not open.")
        sock = self._socket  # type: ignore[assignment]
        old_timeout = sock.gettimeout()  # type: ignore[union-attr]
        try:
            if timeout is not None:
                sock.settimeout(max(timeout.total_seconds(), 0.0))  # type: ignore[union-attr]
            try:
                data, addr = sock.recvfrom(65535)  # type: ignore[union-attr]
                return (data, addr)
            except socket.timeout:
                return None
            except OSError as exc:
                # Treat transient "no data right now" / "interrupted
                # syscall" as timeout-like; surface real network
                # errors to callers (Qodo PR 17 pass 8 #2).
                if isinstance(exc, (BlockingIOError, InterruptedError)):
                    return None
                raise
        finally:
            try:
                sock.settimeout(old_timeout)  # type: ignore[union-attr]
            except Exception:
                pass

    def _notify_status(self, is_open: bool, error: Optional[BaseException]) -> None:
        _notify(
            self._status_handlers,
            TransportStatusEventArgs(
                is_connected=is_open,
                connection_info=self.connection_info,
                error=error,
            ),
        )

    def _raise_if_disposed(self) -> None:
        if self._disposed:
            raise RuntimeError("UdpTransport has been closed.")


# ---- HidLibraryTransport -----------------------------------------------------


class HidLibraryTransport:
    """HID transport backed by an injectable :class:`HidPlatform`.

    Ports the C# ``HidLibraryTransport``. Uses two
    :class:`threading.Lock` instances (mirrors the upstream
    ``SemaphoreSlim(1, 1)`` connection + io locks) so disconnects can
    happen while a long read is still draining without re-entering.

    Default :class:`HidPlatform` is :class:`HidApiPlatform` (lazy-loads
    the ``hid`` package on first use); pass a fake to avoid the
    dependency in tests.
    """

    _DEFAULT_READ_TIMEOUT = timedelta(seconds=10)
    _DEFAULT_WRITE_TIMEOUT = timedelta(seconds=10)

    def __init__(self, platform: Optional[HidPlatform] = None) -> None:
        self._platform = platform if platform is not None else HidApiPlatform()
        self._connected_device: Optional["_HidConnectedDevice"] = None
        self._disposed = False
        self._read_timeout = self._DEFAULT_READ_TIMEOUT
        self._write_timeout = self._DEFAULT_WRITE_TIMEOUT
        self._connection_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._vendor_id: Optional[int] = None
        self._product_id: Optional[int] = None
        self._serial_number: Optional[str] = None
        self._device_path: Optional[str] = None

    @property
    def is_connected(self) -> bool:
        return (
            not self._disposed
            and self._connected_device is not None
            and self._connected_device.is_connected
        )

    @property
    def vendor_id(self) -> Optional[int]:
        return self._vendor_id

    @property
    def product_id(self) -> Optional[int]:
        return self._product_id

    @property
    def serial_number(self) -> Optional[str]:
        return self._serial_number

    @property
    def device_path(self) -> Optional[str]:
        return self._device_path

    @property
    def read_timeout(self) -> timedelta:
        return self._read_timeout

    @read_timeout.setter
    def read_timeout(self, value: timedelta) -> None:
        _validate_positive_timeout(value, "read_timeout")
        self._read_timeout = value

    @property
    def write_timeout(self) -> timedelta:
        return self._write_timeout

    @write_timeout.setter
    def write_timeout(self, value: timedelta) -> None:
        _validate_positive_timeout(value, "write_timeout")
        self._write_timeout = value

    def connect(
        self,
        vendor_id: int,
        product_id: int,
        serial_number: Optional[str] = None,
    ) -> None:
        self._raise_if_disposed()
        _validate_usb_identifier(vendor_id, "vendor_id")
        _validate_usb_identifier(product_id, "product_id")

        with self._connection_lock:
            if self.is_connected:
                return

            serial_filter = (serial_number or "").strip() or None
            candidates = [
                d for d in self._platform.enumerate_devices()
                if isinstance(d, HidDeviceInfo)
                and d.vendor_id == vendor_id
                and d.product_id == product_id
                and (
                    serial_filter is None
                    or (d.serial_number or "").strip().lower() == serial_filter.lower()
                )
            ]
            candidates.sort(key=lambda d: d.device_path)

            if not candidates:
                target = (
                    f"VID=0x{vendor_id:04X}, PID=0x{product_id:04X}"
                    if serial_filter is None
                    else f"VID=0x{vendor_id:04X}, PID=0x{product_id:04X}, "
                         f"Serial={serial_filter}"
                )
                raise IOError(f"No HID device found for {target}.")

            chosen = candidates[0]
            # Use getattr to detect the optional platform capability.
            # Catching AttributeError around the call would also
            # silently swallow any internal AttributeError raised
            # *inside* open_device (Qodo PR 17 pass 8 #1).
            open_device = getattr(self._platform, "open_device", None)
            try:
                if callable(open_device):
                    device = open_device(chosen)
                else:
                    # Default platform doesn't expose open_device;
                    # build the canonical hidapi-backed handle.
                    device = _HidConnectedDevice.open_default(chosen)
            except Exception as exc:
                raise IOError("Failed to open HID device.") from exc

            if not device.is_connected:
                device.close()
                raise IOError(
                    "HID device did not report a connected state after open."
                )

            self._connected_device = device
            self._vendor_id = chosen.vendor_id
            self._product_id = chosen.product_id
            self._serial_number = chosen.serial_number
            self._device_path = chosen.device_path

    def write(self, data: bytes) -> None:
        if data is None:  # type: ignore[unreachable]
            raise TypeError("data must not be None.")
        if len(data) == 0:
            raise ValueError("Data cannot be empty.")
        self._raise_if_disposed()
        with self._io_lock:
            device = self._get_connected_device()
            success = device.write(data, self._timeout_ms(self._write_timeout))
            if not success:
                raise IOError("HID write failed.")

    def read(self, timeout: Optional[timedelta] = None) -> bytes:
        self._raise_if_disposed()
        effective = timeout if timeout is not None else self._read_timeout
        _validate_positive_timeout(effective, "timeout")
        with self._io_lock:
            device = self._get_connected_device()
            return device.read(self._timeout_ms(effective))

    def disconnect(self) -> None:
        if self._disposed:
            return
        with self._connection_lock:
            if self._connected_device is None:
                return
            close_exc: Optional[BaseException] = None
            with self._io_lock:
                try:
                    self._connected_device.close()
                except Exception as exc:
                    close_exc = exc
            self._clear_connection_state()
            if close_exc is not None:
                raise IOError("Failed to close HID device.") from close_exc

    def close(self) -> None:
        if self._disposed:
            return
        # Hold both locks so concurrent connect() / read() / write()
        # can't observe a half-cleared state mid-close. Mirrors the
        # locking shape disconnect() already uses (Qodo PR 17 pass 9 #3).
        with self._connection_lock:
            with self._io_lock:
                device = self._connected_device
                self._clear_connection_state()
                self._disposed = True
                try:
                    if device is not None:
                        device.close()
                except Exception:
                    pass

    @staticmethod
    def _timeout_ms(value: timedelta) -> int:
        ms = int(value.total_seconds() * 1000)
        return min(max(ms, 1), 2_147_483_647)

    def _clear_connection_state(self) -> None:
        self._connected_device = None
        self._vendor_id = None
        self._product_id = None
        self._serial_number = None
        self._device_path = None

    def _get_connected_device(self) -> "_HidConnectedDevice":
        if self._connected_device is None or not self._connected_device.is_connected:
            raise RuntimeError("HID transport is not connected.")
        return self._connected_device

    def _raise_if_disposed(self) -> None:
        if self._disposed:
            raise RuntimeError("HidLibraryTransport has been closed.")


class _HidConnectedDevice:
    """Default open-device adapter backed by the ``hid`` package.

    Mirrors the upstream ``IHidTransportDevice`` shape with three
    methods (:meth:`write`, :meth:`read`, :meth:`close`) plus
    :attr:`is_connected`. Tests provide their own duck-typed device by
    teaching the supplied :class:`HidPlatform` to return one from
    ``open_device(info)``.
    """

    def __init__(self, hid_module, handle, info: HidDeviceInfo) -> None:
        self._hid = hid_module
        self._handle = handle
        self._info = info
        self._closed = False

    @classmethod
    def open_default(cls, info: HidDeviceInfo) -> "_HidConnectedDevice":
        try:
            import hid  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "HID transport requires the 'hid' package "
                "(`pip install hid`). Pass a custom HidPlatform with an "
                "open_device(info) method to avoid this dependency."
            ) from exc
        handle = hid.device()
        # OS-provided HID paths can contain non-UTF-8 bytes on Linux
        # (filesystem-encoded). `surrogateescape` round-trips those
        # bytes losslessly so a strict UTF-8 encode doesn't refuse
        # to open an otherwise-valid device (Qodo PR 17 pass 31 #1).
        if isinstance(info.device_path, bytes):
            path = info.device_path
        else:
            path = str(info.device_path).encode(
                "utf-8", errors="surrogateescape"
            )
        handle.open_path(path)
        return cls(hid, handle, info)

    @property
    def is_connected(self) -> bool:
        return not self._closed

    def write(self, data: bytes, timeout_ms: int) -> bool:
        # hidapi's `write` returns the number of bytes written, or -1
        # on error. Return True iff bytes-written matches input length.
        del timeout_ms  # hidapi write is non-blocking on most platforms
        try:
            written = self._handle.write(data)
        except Exception:
            return False
        return written == len(data)

    def read(self, timeout_ms: int) -> bytes:
        # hidapi `read(max_length, timeout_ms)` returns a list[int]
        # or empty list on timeout. Read up to a HID full-speed report
        # (64 bytes) plus headroom. Positional args: the C-extension
        # binding doesn't support kwargs across all hidapi versions
        # (Qodo PR 17 pass 6 #4). Return ``b""`` on timeout to match
        # pyserial / standard Python I/O semantics — exceptions for
        # routine no-data conditions force callers to wrap every read
        # in try/except (Qodo PR 17 pass 21 #1).
        result = self._handle.read(64, timeout_ms)
        if not result:
            return b""
        return bytes(result)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.close()
        except Exception:
            pass


# Re-export for convenient ``from daqifi.transport import io`` discovery.
del io
