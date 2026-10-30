"""Device-discovery finders: HID, Serial, WiFi.

Ports the three concrete IDeviceFinder implementations from
``Daqifi.Core/Device/Discovery/`` (HidDeviceFinder, SerialDeviceFinder,
WiFiDeviceFinder @ upstream e8d84af).

Distinct from :class:`daqifi.discovery.DeviceFinder` -- that class is a
flat platform-specific port enumerator used by NyquistDevice's
high-level API. The classes here are the lower-level
contract-implementing finders that match the C# Daqifi.Core surface
and are useful when integrating with the new
:class:`daqifi.devices.DaqifiStreamingDevice` flow.

All sync per project convention. Threading + UDP socket + serial
probing are real -- the user's ``tests/integration/`` suite (run
against actual hardware) is the primary validation path for the probe
loops; this module's unit tests cover API contract, event wiring, and
mocked success/failure paths.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Callable, List, Optional, Tuple

from ._internal.comm import HidDeviceInfo
from ._internal.scpi import StreamInterface
from ._internal import scpi_commands
from ._internal.hid_enumerator import HidLibraryDeviceEnumerator
from ._internal.usb_ports import daqifi_serial_ports, run_port_probe
from .transport import UdpTransport


__all__ = [
    "ConnectionType",
    "DeviceType",
    "DeviceInfo",
    "DeviceDiscoveredEventArgs",
    "HidDeviceFinder",
    "SerialDeviceFinder",
    "WiFiDeviceFinder",
]


class ConnectionType:
    """Mirrors the C# ``ConnectionType`` enum -- string values for clarity."""
    SERIAL = "serial"
    WIFI = "wifi"
    HID = "hid"


class DeviceType:
    """Mirrors the C# ``DeviceType`` enum slim subset used by finders."""
    UNKNOWN = "unknown"
    NYQUIST = "nyquist"


@dataclass(frozen=True)
class DeviceInfo:
    """Discovery-time metadata about a candidate DAQiFi device.

    Mirrors the C# ``IDeviceInfo`` surface (also called ``DeviceInfo``).
    Different shape from :class:`daqifi.discovery.DeviceInfo` (which is
    NyquistDevice-flavoured); kept distinct to avoid breaking that API.
    """
    name: str = ""
    serial_number: str = ""
    firmware_version: str = ""
    connection_type: str = ConnectionType.SERIAL
    device_type: str = DeviceType.UNKNOWN
    port_name: Optional[str] = None
    ip_address: Optional[str] = None
    port: Optional[int] = None
    device_path: Optional[str] = None
    is_power_on: bool = True
    # WiFi-only: hyphen-separated upper-hex MAC ("AA-BB-CC-DD-EE-FF")
    # decoded from the device's protobuf discovery response. Empty
    # for non-WiFi finders or when the device pre-dates protobuf
    # discovery responses.
    mac_address: str = ""
    # WiFi-only: dotted-decimal IPv4 of the local interface that
    # received the discovery broadcast (e.g. "192.168.1.42"). Lets
    # callers route a follow-up TCP connection back through the same
    # interface in multi-homed setups. Empty for non-WiFi finders
    # or when discovery didn't capture per-interface origin info.
    # Mirrors C# ``IDeviceInfo.LocalInterfaceAddress``.
    local_interface_address: str = ""


@dataclass(frozen=True)
class DeviceDiscoveredEventArgs:
    """Event payload for ``DeviceDiscovered`` callbacks."""
    device_info: DeviceInfo


# ---- HidDeviceFinder ---------------------------------------------------------


class HidDeviceFinder:
    """Discovers DAQiFi devices in USB HID bootloader mode.

    Ports the C# ``HidDeviceFinder``. Defaults to filtering for the
    DAQiFi bootloader VID/PID (``0x04D8`` / ``0x003C``); set
    :attr:`vendor_id_filter` / :attr:`product_id_filter` to ``None``
    to enumerate everything.
    """

    DEFAULT_VENDOR_ID = 0x04D8
    DEFAULT_PRODUCT_ID = 0x003C

    def __init__(self, hid_enumerator: Optional[Any] = None) -> None:
        self._enumerator = hid_enumerator if hid_enumerator is not None else HidLibraryDeviceEnumerator()
        self.vendor_id_filter: Optional[int] = self.DEFAULT_VENDOR_ID
        self.product_id_filter: Optional[int] = self.DEFAULT_PRODUCT_ID
        self._lock = threading.Lock()
        self._disposed = False
        self._discovered_handlers: List[
            Callable[[DeviceDiscoveredEventArgs], None]
        ] = []
        self._completed_handlers: List[Callable[[], None]] = []

    def add_device_discovered(
        self, handler: Callable[[DeviceDiscoveredEventArgs], None]
    ) -> None:
        self._discovered_handlers.append(handler)

    def remove_device_discovered(
        self, handler: Callable[[DeviceDiscoveredEventArgs], None]
    ) -> None:
        try:
            self._discovered_handlers.remove(handler)
        except ValueError:
            pass

    def add_discovery_completed(
        self, handler: Callable[[], None]
    ) -> None:
        self._completed_handlers.append(handler)

    def remove_discovery_completed(
        self, handler: Callable[[], None]
    ) -> None:
        try:
            self._completed_handlers.remove(handler)
        except ValueError:
            pass

    def discover(
        self,
        vendor_id_filter: Optional[int] = None,
        product_id_filter: Optional[int] = None,
    ) -> List[DeviceInfo]:
        if self._disposed:
            raise RuntimeError("HidDeviceFinder has been closed.")

        # Use instance-level filters when caller passes None for both.
        if vendor_id_filter is None and product_id_filter is None:
            vendor_id_filter = self.vendor_id_filter
            product_id_filter = self.product_id_filter

        with self._lock:
            hid_devices = list(
                self._enumerator.enumerate(
                    vendor_id=vendor_id_filter,
                    product_id=product_id_filter,
                )
            )
            results: List[DeviceInfo] = []
            for hid in hid_devices:
                info = DeviceInfo(
                    name=hid.product_name if hid.product_name and hid.product_name.strip()
                         else "DAQiFi Bootloader",
                    serial_number=hid.serial_number or "",
                    firmware_version="",
                    connection_type=ConnectionType.HID,
                    device_type=DeviceType.UNKNOWN,
                    device_path=hid.device_path,
                )
                results.append(info)
        # Fire callbacks OUTSIDE the lock — a subscriber that calls
        # back into this finder (or otherwise blocks) under the lock
        # would deadlock the next discover() (Qodo PR 17 pass 24 #1).
        for info in results:
            self._fire_discovered(info)
        self._fire_completed()
        return results

    def close(self) -> None:
        self._disposed = True

    def _fire_discovered(self, info: DeviceInfo) -> None:
        evt = DeviceDiscoveredEventArgs(device_info=info)
        for h in list(self._discovered_handlers):
            try:
                h(evt)
            except Exception:
                pass

    def _fire_completed(self) -> None:
        for h in list(self._completed_handlers):
            try:
                h()
            except Exception:
                pass


# ---- SerialDeviceFinder ------------------------------------------------------


# Constants that mirror upstream's probe-loop behaviour.
_SERIAL_PROBE_TIMEOUT_S = 1.0
_SERIAL_DEVICE_WAKEUP_DELAY_S = 1.0
_SERIAL_RESPONSE_TIMEOUT_S = 4.0
_SERIAL_MAX_RETRIES = 3
_SERIAL_RETRY_INTERVAL_S = 1.0
_SERIAL_POLL_INTERVAL_S = 0.1
_SERIAL_INIT_COMMANDS = 4  # DisableEcho, StopStreaming, TurnOn, PB format
_SERIAL_INIT_COMMAND_DELAY_S = 0.1
# The probe's longest healthy run: wake-up; each init command's write (up to
# the write timeout, _SERIAL_PROBE_TIMEOUT_S) and wait; the response loop,
# whose GetDeviceInfo writes can each block up to the write timeout too; one
# read that starts just before the loop's deadline and runs its full read
# timeout; then a poll interval.
_SERIAL_PROBE_WORST_HEALTHY_S = (
    _SERIAL_DEVICE_WAKEUP_DELAY_S
    + _SERIAL_INIT_COMMANDS * (_SERIAL_PROBE_TIMEOUT_S + _SERIAL_INIT_COMMAND_DELAY_S)
    + _SERIAL_RESPONSE_TIMEOUT_S
    + _SERIAL_MAX_RETRIES * _SERIAL_PROBE_TIMEOUT_S
    + _SERIAL_PROBE_TIMEOUT_S
    + _SERIAL_POLL_INTERVAL_S
)
# The hard timeout adds room for the port open itself (a fresh
# enumeration can take seconds). A probe past it is abandoned.
_SERIAL_PROBE_HARD_TIMEOUT_S = _SERIAL_PROBE_WORST_HEALTHY_S + 3.0
assert _SERIAL_PROBE_HARD_TIMEOUT_S > _SERIAL_PROBE_WORST_HEALTHY_S


class SerialDeviceFinder:
    """Discovers DAQiFi devices on serial ports by SCPI probing.

    Ports the C# ``SerialDeviceFinder``. For each available serial
    port: open at the configured baud rate, send the standard SCPI
    init sequence (DisableEcho / StopStreaming / TurnOn /
    SetProtobufFormat / GetDeviceInfo), and treat a binary protobuf
    response as confirmation of a DAQiFi device.

    The Python port simplifies upstream's protobuf-message dispatcher
    -- here we treat any non-empty binary response (with NULL bytes,
    indicating likely protobuf) as a hit. Hardware-validated runs
    confirm the port-name is correct; richer parsing of the device
    metadata happens in :meth:`daqifi.devices.DaqifiDevice.initialize`
    after the connection is established.
    """

    def __init__(self, baud_rate: int = 9600) -> None:
        if baud_rate <= 0:
            raise ValueError("baud_rate must be positive.")
        self._baud_rate = baud_rate
        self._lock = threading.Lock()
        self._disposed = False
        self._discovered_handlers: List[
            Callable[[DeviceDiscoveredEventArgs], None]
        ] = []
        self._completed_handlers: List[Callable[[], None]] = []

    def add_device_discovered(self, handler):
        self._discovered_handlers.append(handler)

    def remove_device_discovered(self, handler):
        try:
            self._discovered_handlers.remove(handler)
        except ValueError:
            pass

    def add_discovery_completed(self, handler):
        self._completed_handlers.append(handler)

    def remove_discovery_completed(self, handler):
        try:
            self._completed_handlers.remove(handler)
        except ValueError:
            pass

    def discover(
        self,
        port_filter: Optional[List[str]] = None,
    ) -> List[DeviceInfo]:
        """Probe each DAQiFi serial port; return DAQiFi candidates.

        Auto-enumeration lists only USB serial ports with the DAQiFi
        application's VID/PID (04D8:F794), plus CDC-ACM ports whose IDs
        can't be read, so the probe's SCPI (including a power-on) never
        goes to an unrelated device. ``port_filter`` overrides it: the
        ports given are probed as they are. Each probe is bounded by a hard
        timeout, so one hung port can't block discovery.
        """
        if self._disposed:
            raise RuntimeError("SerialDeviceFinder has been closed.")

        with self._lock:
            if port_filter is not None:
                ports = list(port_filter)
            else:
                ports = daqifi_serial_ports()

            discovered: List[DeviceInfo] = []
            for port_name in ports:
                # Failures and overruns give None (upstream: skip ports
                # that fail).
                info = run_port_probe(
                    port_name, self._try_get_device_info, _SERIAL_PROBE_HARD_TIMEOUT_S
                )
                if info is not None:
                    discovered.append(info)
        # Fire callbacks OUTSIDE the lock — same deadlock concern
        # as the HID finder (Qodo PR 17 pass 25 #1 / pass 24 #1).
        for info in discovered:
            self._fire_discovered(info)
        self._fire_completed()
        return discovered

    def _try_get_device_info(
        self, port_name: str, cancelled: Optional[threading.Event] = None
    ) -> Optional[DeviceInfo]:
        """Probe one port and return DeviceInfo if it looks like DAQiFi.

        Simplified vs upstream's full TaskCompletionSource flow: open
        the port, send the SCPI init + GetDeviceInfo, read the response
        for ``_SERIAL_RESPONSE_TIMEOUT_S``. A non-empty binary response
        (containing any null byte) is treated as a protobuf hit.
        """
        import serial

        try:
            with serial.Serial(
                port=port_name,
                baudrate=self._baud_rate,
                timeout=_SERIAL_PROBE_TIMEOUT_S,
                write_timeout=_SERIAL_PROBE_TIMEOUT_S,
            ) as port:
                def _abandoned() -> bool:
                    # Given up on by discover() (run_port_probe): touch
                    # nothing on the device; leaving the block closes it.
                    return cancelled is not None and cancelled.is_set()

                if _abandoned():
                    return None
                # DTR-up wakes the device.
                port.dtr = True
                time.sleep(_SERIAL_DEVICE_WAKEUP_DELAY_S)

                # Drop any bytes the OS / device left in either buffer
                # before the probe started. Without this, stale data
                # from a prior session can be misread as a successful
                # protobuf response (false-positive discovery), or
                # garble our SCPI init writes (Qodo PR 17 pass 13 #3).
                try:
                    if hasattr(port, "reset_input_buffer"):
                        port.reset_input_buffer()
                    if hasattr(port, "reset_output_buffer"):
                        port.reset_output_buffer()
                except Exception:
                    pass

                # SCPI init sequence (matches DaqifiDevice.initialize).
                for command in (
                    scpi_commands.disable_device_echo(),
                    scpi_commands.stop_streaming(),
                    scpi_commands.turn_device_on(),
                    scpi_commands.set_protobuf_stream_format(),
                ):
                    if _abandoned():
                        return None
                    port.write(command.get_bytes())
                    time.sleep(_SERIAL_INIT_COMMAND_DELAY_S)

                # GetDeviceInfo with retry. Use monotonic clock so a
                # wall-clock jump (NTP / DST) can't corrupt the
                # deadline math (Qodo PR 17 pass 25 #4).
                deadline = time.monotonic() + _SERIAL_RESPONSE_TIMEOUT_S
                last_send = 0.0
                retry_count = 0
                received = bytearray()
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if now - last_send >= _SERIAL_RETRY_INTERVAL_S and retry_count < _SERIAL_MAX_RETRIES:
                        if _abandoned():
                            return None
                        port.write(scpi_commands.get_device_info().get_bytes())
                        last_send = now
                        retry_count += 1
                    chunk = port.read(256)
                    if chunk:
                        received.extend(chunk)
                        # A protobuf-shaped response contains null bytes.
                        if b"\x00" in received:
                            return DeviceInfo(
                                name=port_name,
                                serial_number="",
                                firmware_version="",
                                connection_type=ConnectionType.SERIAL,
                                device_type=DeviceType.UNKNOWN,
                                port_name=port_name,
                            )
                    time.sleep(_SERIAL_POLL_INTERVAL_S)
        except (serial.SerialException, OSError, ValueError):
            return None
        return None

    def close(self) -> None:
        self._disposed = True

    def _fire_discovered(self, info: DeviceInfo) -> None:
        evt = DeviceDiscoveredEventArgs(device_info=info)
        for h in list(self._discovered_handlers):
            try:
                h(evt)
            except Exception:
                pass

    def _fire_completed(self) -> None:
        for h in list(self._completed_handlers):
            try:
                h()
            except Exception:
                pass


# ---- WiFiDeviceFinder --------------------------------------------------------


_DAQIFI_FINDER_QUERY = b"DAQiFi?\r\n"
_DEFAULT_DISCOVERY_PORT = 30303
_WIFI_DEFAULT_TIMEOUT_S = 5.0
_POWER_EVENT_MARKER = "Power event occurred"
_DEFAULT_SCPI_TCP_PORT = 9760


def _read_varint32(data: bytes) -> Tuple[Optional[int], int]:
    """Decode a protobuf varint32 from the head of ``data``.

    Returns ``(value, bytes_read)`` on success, or ``(None, bytes_read)``
    if the encoding is incomplete (truncated) or malformed (varint
    longer than 5 bytes). Inlined here rather than imported from
    `daqifi.communication._try_read_length_prefix` to keep
    `device_finders` decoupled from another module's private helper —
    the encoding is a 5-line spec and duplicating it is cheaper than
    coupling to a private name across modules.
    """
    value = 0
    shift = 0
    for i in range(5):
        if i >= len(data):
            return None, i  # truncated
        b = data[i]
        value |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return value, i + 1
        shift += 7
    return None, 5  # malformed (no terminating byte in 5 bytes)


def _try_parse_protobuf_response(data: bytes) -> Optional[Any]:
    """Decode a UDP discovery response as a `DaqifiOutMessage`.

    Mirrors the C# upstream's
    ``DaqifiOutMessage.Parser.ParseDelimitedFrom(stream)`` —
    firmware emits a varint length prefix followed by the protobuf
    payload. Returns ``None`` if neither the length-prefixed nor a
    bare-payload decode yields a message with at least one identity
    field; callers should then fall back to the legacy text parse.
    """
    if not data:
        return None
    try:
        from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
    except Exception:
        return None

    # Length-prefixed first — that's what the firmware sends.
    # UDP discovery datagrams carry exactly one frame, so require an
    # exact match on `prefix + length == len(data)` — a bare payload
    # whose first byte happens to look like a valid varint length
    # would otherwise enter the delimited path with leftover bytes
    # silently truncated (Qodo PR 19 review bug #6).
    length, prefix_bytes = _read_varint32(data)
    if length is not None and length > 0:
        if prefix_bytes + length == len(data):
            try:
                msg = DaqifiOutMessage.FromString(data[prefix_bytes:])
                if _has_identity_field(msg):
                    return msg
            except Exception:
                pass

    # Fallback: some firmware variants may send the bare payload.
    # Reject all-default decodes — random text often parses to an
    # empty protobuf because every field is optional in proto3.
    try:
        msg = DaqifiOutMessage.FromString(data)
    except Exception:
        return None
    return msg if _has_identity_field(msg) else None


def _has_identity_field(message: Any) -> bool:
    """Did protobuf decoding actually populate an identifying field?

    Used as the false-positive guard for `_try_parse_protobuf_response`
    — proto3 has no required fields, so plain text often decodes to
    a zero-valued message that we must reject.
    """
    return bool(
        (getattr(message, "host_name", "") or "").strip()
        or (getattr(message, "device_pn", "") or "").strip()
        or (getattr(message, "device_fw_rev", "") or "").strip()
        or getattr(message, "device_sn", 0)
        or getattr(message, "mac_addr", b"")
    )


def _coerce_serial_number(value: Any) -> str:
    """Convert a `device_sn` field (uint64 or str) to a display string."""
    if isinstance(value, str):
        return value.strip()
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return ""
    return str(as_int) if as_int else ""


def _coerce_device_port(value: Any) -> int:
    """Validate `device_port` and fall back to firmware default."""
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        as_int = 0
    if 1 <= as_int <= 65535:
        return as_int
    return _DEFAULT_SCPI_TCP_PORT


def _device_type_from_part_number(device_pn: str) -> str:
    """Map firmware-reported part number to the finder's DeviceType."""
    pn = (device_pn or "").strip().lower()
    if pn.startswith("nq"):
        return DeviceType.NYQUIST
    return DeviceType.UNKNOWN


class WiFiDeviceFinder:
    """Discovers DAQiFi devices on the LAN via UDP broadcast.

    Ports the C# ``WiFiDeviceFinder``. Sends the ``DAQiFi?`` query to
    the broadcast address on port 30303 (overridable), then collects
    responses until ``timeout`` elapses. Each response yields one
    :class:`DeviceInfo` with the device's IP + port populated.

    The native firmware also supports a ``"Discovery: Who is out
    there?\\r\\n"`` query; that variant is exposed via
    :data:`alternate_query` if callers want to probe both.
    """

    DAQIFI_FINDER_QUERY = _DAQIFI_FINDER_QUERY
    NATIVE_FINDER_QUERY = b"Discovery: Who is out there?\r\n"

    def __init__(self, discovery_port: int = _DEFAULT_DISCOVERY_PORT) -> None:
        if discovery_port < 1 or discovery_port > 65535:
            raise ValueError("discovery_port must be 1..65535")
        self._discovery_port = discovery_port
        self._lock = threading.Lock()
        self._disposed = False
        self._discovered_handlers: List[
            Callable[[DeviceDiscoveredEventArgs], None]
        ] = []
        self._completed_handlers: List[Callable[[], None]] = []

    def add_device_discovered(self, handler):
        self._discovered_handlers.append(handler)

    def remove_device_discovered(self, handler):
        try:
            self._discovered_handlers.remove(handler)
        except ValueError:
            pass

    def add_discovery_completed(self, handler):
        self._completed_handlers.append(handler)

    def remove_discovery_completed(self, handler):
        try:
            self._completed_handlers.remove(handler)
        except ValueError:
            pass

    def discover(
        self,
        timeout: timedelta = timedelta(seconds=_WIFI_DEFAULT_TIMEOUT_S),
        udp_transport: Optional[UdpTransport] = None,
    ) -> List[DeviceInfo]:
        """Send broadcast query, collect responses for ``timeout``.

        ``udp_transport`` lets callers inject an opened transport
        (useful for tests). When ``None`` the finder enumerates real
        NICs (port of daqifi-core PR #179/#180) and binds one
        :class:`UdpTransport` per NIC so discovery reaches devices on
        any LAN-attached interface, not just the default route. If
        no real NICs are detected (e.g. ``ifaddr`` unavailable), it
        falls back to a single wildcard-bound transport.
        """
        if self._disposed:
            raise RuntimeError("WiFiDeviceFinder has been closed.")

        # `time.monotonic()` is immune to wall-clock jumps — without
        # this a NTP step or DST transition mid-discover would either
        # extend the deadline forever or trip it instantly (Qodo PR
        # 17 pass 25 #4).
        deadline = time.monotonic() + max(timeout.total_seconds(), 0.0)
        # De-dupe by MAC when the device's protobuf carries one
        # (matches the C# IsDuplicateDevice helper); else fall back
        # to source IP. Using a single set with a "mac:..." / "ip:..."
        # prefix keeps the two namespaces from colliding.
        seen_keys: set = set()
        discovered: List[DeviceInfo] = []
        dedup_lock = threading.Lock()
        try:
            with self._lock:
                if udp_transport is not None:
                    # Caller-provided transport (test injection or
                    # legacy single-socket override) — preserve the
                    # existing single-socket path so existing tests
                    # and callers see no change.
                    self._collect_on_transport(
                        udp_transport,
                        deadline,
                        seen_keys,
                        discovered,
                        dedup_lock,
                        broadcast_addr=None,
                    )
                else:
                    # Multi-NIC path — enumerate real interfaces and
                    # broadcast on each in parallel.
                    self._discover_per_nic(
                        deadline, seen_keys, discovered, dedup_lock
                    )
        finally:
            # Fire callbacks OUTSIDE the lock — same deadlock concern
            # as the HID/Serial finders (Qodo PR 17 pass 25 #2). Run
            # in finally so subscribers waiting on _fire_completed
            # still hear it even if the discovery loop itself raised
            # (Qodo PR 17 pass 46 #2). Snapshot under dedup_lock so
            # a still-running per-NIC daemon thread (one that didn't
            # quite hit the join timeout) can't append mid-iteration.
            with dedup_lock:
                discovered_snapshot = list(discovered)
            for info in discovered_snapshot:
                self._fire_discovered(info)
            self._fire_completed()
        return discovered_snapshot

    def _discover_per_nic(
        self,
        deadline: float,
        seen_keys: set,
        discovered: List[DeviceInfo],
        dedup_lock: threading.Lock,
    ) -> None:
        """Open one UdpTransport per real NIC, broadcast on each, and
        run their receive loops concurrently until ``deadline``.
        Falls back to a single wildcard-bound transport if no real
        NICs are discoverable (e.g. ``ifaddr`` not installed).
        """
        from ._internal.nic import enumerate_real_nics
        nics = enumerate_real_nics()
        if not nics:
            # Graceful degradation: behave as before so single-NIC /
            # ifaddr-missing hosts still discover.
            transport = UdpTransport(local_port=0)
            try:
                transport.open()
                self._collect_on_transport(
                    transport, deadline, seen_keys, discovered,
                    dedup_lock, broadcast_addr=None,
                )
            finally:
                try:
                    transport.close()
                except Exception:
                    pass
            return

        # One transport + one thread per NIC. Each thread owns its
        # transport's lifecycle so a slow / failing NIC can't leak
        # the others' sockets.
        threads: List[threading.Thread] = []
        for nic in nics:
            t = threading.Thread(
                target=self._discover_on_one_nic,
                args=(nic, deadline, seen_keys, discovered, dedup_lock),
                daemon=True,
                name=f"WiFiDiscover-{nic.name}",
            )
            t.start()
            threads.append(t)

        # Bound the TOTAL join wait on a single absolute deadline +
        # a small grace, so a misbehaving thread can't pin the
        # discover() call past its nominal timeout — and 4 NICs
        # don't take 4 × budget when each thread runs to its own
        # full timeout. Daemon threads will be torn down when the
        # interpreter exits if they outlive this join.
        join_deadline = deadline + 1.0
        for t in threads:
            remaining = max(join_deadline - time.monotonic(), 0.0)
            t.join(timeout=remaining)

    def _discover_on_one_nic(
        self,
        nic,
        deadline: float,
        seen_keys: set,
        discovered: List[DeviceInfo],
        dedup_lock: threading.Lock,
    ) -> None:
        """Per-NIC worker: open a transport bound to the NIC's IP,
        broadcast, drain the receive loop until ``deadline``, close.
        Any exception is swallowed locally — one bad NIC must not
        kill discovery on the rest.
        """
        transport = UdpTransport(local_port=0, local_ip=nic.ip)
        try:
            try:
                transport.open()
            except Exception:
                # Bind failure (e.g. NIC went away between
                # enumeration and open) — skip silently.
                return
            try:
                self._collect_on_transport(
                    transport,
                    deadline,
                    seen_keys,
                    discovered,
                    dedup_lock,
                    broadcast_addr=nic.broadcast,
                    local_interface_address=nic.ip,
                )
            except Exception:
                # Receive-loop crash on one NIC is non-fatal for the
                # others. Logging would require a logger import
                # higher in the file; intentionally silent for now.
                pass
        finally:
            try:
                transport.close()
            except Exception:
                pass

    def _collect_on_transport(
        self,
        transport: UdpTransport,
        deadline: float,
        seen_keys: set,
        discovered: List[DeviceInfo],
        dedup_lock: threading.Lock,
        broadcast_addr: Optional[str],
        local_interface_address: str = "",
    ) -> None:
        """Send the two finder queries and drain the receive loop on
        a single transport until ``deadline``. Shared seen_keys /
        discovered list let multiple per-NIC callers cooperate on
        de-duplication. ``broadcast_addr=None`` falls back to
        255.255.255.255 (legacy single-socket behavior).

        ``local_interface_address`` is the IP the socket is bound to (the
        NIC a reply arrived on), recorded on each DeviceInfo so a later TCP
        connection can leave by the same NIC. Empty for a socket bound to
        any interface.
        """
        if broadcast_addr is None:
            transport.send_broadcast(self.DAQIFI_FINDER_QUERY, self._discovery_port)
            transport.send_broadcast(self.NATIVE_FINDER_QUERY, self._discovery_port)
        else:
            transport.send_broadcast_to(
                self.DAQIFI_FINDER_QUERY, (broadcast_addr, self._discovery_port)
            )
            transport.send_broadcast_to(
                self.NATIVE_FINDER_QUERY, (broadcast_addr, self._discovery_port)
            )

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = transport.receive(
                timeout=timedelta(seconds=min(0.5, remaining))
            )
            if result is None:
                continue
            data, (ip, port) = result
            text = data.decode("utf-8", errors="replace")
            if _POWER_EVENT_MARKER in text:
                continue
            if (
                self.DAQIFI_FINDER_QUERY in data
                or self.NATIVE_FINDER_QUERY in data
            ):
                continue

            info = replace(
                self._build_device_info(data, text, ip),
                local_interface_address=local_interface_address,
            )
            key = (
                f"mac:{info.mac_address}"
                if info.mac_address
                else f"ip:{ip}"
            )
            # Lock the seen_keys check + append so concurrent NIC
            # workers don't both record the same device.
            with dedup_lock:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                discovered.append(info)

    @staticmethod
    def _build_device_info(data: bytes, text: str, ip: str) -> DeviceInfo:
        """Translate one UDP datagram into a `DeviceInfo`.

        Tries the firmware's length-prefixed protobuf framing first
        (matches C# `WiFiDeviceFinder.ParseDeviceInfo`), then falls
        back to the legacy text-only response shape so older firmware
        and stub devices that just echo "DaqifiNyq1\\r\\n" still
        discover.
        """
        pb = _try_parse_protobuf_response(data)
        if pb is not None:
            try:
                from daqifi.network import get_mac_address_string
                mac = get_mac_address_string(pb)
            except Exception:
                mac = ""
            host_name = (getattr(pb, "host_name", "") or "").strip()
            fw_rev = (getattr(pb, "device_fw_rev", "") or "").strip()
            try:
                is_power_on = int(getattr(pb, "pwr_status", 1)) == 1
            except (TypeError, ValueError):
                is_power_on = True
            return DeviceInfo(
                name=host_name or f"DAQiFi-{ip}",
                serial_number=_coerce_serial_number(getattr(pb, "device_sn", 0)),
                firmware_version=fw_rev,
                connection_type=ConnectionType.WIFI,
                device_type=_device_type_from_part_number(getattr(pb, "device_pn", "")),
                ip_address=ip,
                port=_coerce_device_port(getattr(pb, "device_port", 0)),
                mac_address=mac,
                is_power_on=is_power_on,
            )

        stripped = text.strip()
        return DeviceInfo(
            name=stripped.splitlines()[0] if stripped else f"DAQiFi-{ip}",
            serial_number="",
            firmware_version="",
            connection_type=ConnectionType.WIFI,
            device_type=DeviceType.UNKNOWN,
            ip_address=ip,
            port=_DEFAULT_SCPI_TCP_PORT,
            mac_address="",
        )

    def probe_ip(
        self,
        ip: str,
        port: int = _DEFAULT_SCPI_TCP_PORT,
        timeout_s: float = 5.0,
    ) -> Optional[DeviceInfo]:
        """Probe a known device IP directly via TCP, bypassing UDP broadcast.

        Useful when UDP broadcast doesn't reach back (e.g. WSL2's
        mirrored networking drops inbound broadcast responses) but the
        operator already knows the device's IP — closes #72.

        Opens a short-lived TCP connection to ``ip:port``, runs the
        standard initialize sequence so the protobuf metadata
        populates, and returns a single :class:`DeviceInfo` shaped the
        same way :meth:`discover` would have. Returns ``None`` if the
        connection fails or the device is unresponsive.

        Does NOT fire the ``device_discovered`` / ``discovery_completed``
        callbacks — this is a synchronous reachability probe, not a
        discovery scan.
        """
        if self._disposed:
            raise RuntimeError("WiFiDeviceFinder has been closed.")
        # Lazy import — daqifi.devices imports protocol/network and
        # finders are typically imported earlier in user code.
        from daqifi.devices import DaqifiDeviceFactory
        from daqifi._internal.comm import ConnectionRetryOptions
        # Apply timeout_s to the TCP connect too, not just initialize().
        # Without retry_options the connect would use the 5 s default,
        # so a probe with `timeout_s=0.5` could still block 5 s on an
        # unreachable IP (Qodo PR #76 /agentic_review pass 1 bug #2).
        retry = ConnectionRetryOptions(
            enabled=False,
            max_attempts=1,
            connection_timeout=timeout_s,
        )
        try:
            # initialize_device=False so we can pass the explicit
            # timeout_s to initialize() — the auto-init path uses the
            # default 5.0 s ceiling, which would override the caller's
            # choice of a tighter probe timeout.
            #
            # ValueError + TypeError propagate (caller passed bad input
            # — those are misconfiguration, not "device unreachable").
            # connect_tcp validates host/port explicitly; swallowing
            # those would silently mask DAQIFI_DEVICE_LAN_IP typos
            # (Qodo PR #76 /agentic_review pass 2 bug #1).
            device = DaqifiDeviceFactory.connect_tcp(
                host=ip, port=port,
                initialize_device=False,
                retry_options=retry,
            )
        except (ValueError, TypeError):
            raise
        except Exception:
            return None
        try:
            try:
                device.initialize(response_timeout_s=timeout_s)
            except Exception:
                return None
            meta = device.metadata
            host_name = (meta.host_name or "").strip()
            pn = (meta.part_number or "").strip()
            # Prefer the device-reported port from protobuf metadata so
            # probe_ip output matches discover() output for the same
            # device. ``_coerce_device_port`` handles the 0/out-of-range
            # fallback to the firmware's default — feed the caller's
            # ``port`` arg into it when the protobuf field is empty so
            # that path also gets validation, not a raw passthrough
            # (Qodo PR #76 pass 4 bug #3 — comment/code mismatch).
            reported_port = _coerce_device_port(
                meta.device_port if meta.device_port else port
            )
            return DeviceInfo(
                name=host_name or f"DAQiFi-{ip}",
                serial_number=meta.serial_number,
                firmware_version=meta.firmware_version,
                connection_type=ConnectionType.WIFI,
                device_type=_device_type_from_part_number(pn),
                ip_address=ip,
                port=reported_port,
                mac_address=meta.mac_address,
            )
        finally:
            try:
                device.close()
            except Exception:
                pass

    def close(self) -> None:
        self._disposed = True

    def _fire_discovered(self, info: DeviceInfo) -> None:
        evt = DeviceDiscoveredEventArgs(device_info=info)
        for h in list(self._discovered_handlers):
            try:
                h(evt)
            except Exception:
                pass

    def _fire_completed(self) -> None:
        for h in list(self._completed_handlers):
            try:
                h()
            except Exception:
                pass
