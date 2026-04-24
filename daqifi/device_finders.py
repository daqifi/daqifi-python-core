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
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, List, Optional

from ._internal.comm import HidDeviceInfo
from ._internal.scpi import StreamInterface
from ._internal import scpi_commands
from ._internal.hid_enumerator import HidLibraryDeviceEnumerator
from .transport import SerialStreamTransport, UdpTransport


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
        """Probe each available serial port; return DAQiFi candidates.

        ``port_filter`` overrides auto-enumeration -- pass an explicit
        list to skip ports of known non-DAQiFi devices.
        """
        if self._disposed:
            raise RuntimeError("SerialDeviceFinder has been closed.")

        with self._lock:
            if port_filter is not None:
                ports = list(port_filter)
            else:
                ports = SerialStreamTransport.get_available_port_names()

            discovered: List[DeviceInfo] = []
            for port_name in ports:
                try:
                    info = self._try_get_device_info(port_name)
                except Exception:
                    # Mirrors upstream "skip ports that fail" comment.
                    info = None
                if info is not None:
                    discovered.append(info)
        # Fire callbacks OUTSIDE the lock — same deadlock concern
        # as the HID finder (Qodo PR 17 pass 25 #1 / pass 24 #1).
        for info in discovered:
            self._fire_discovered(info)
        self._fire_completed()
        return discovered

    def _try_get_device_info(self, port_name: str) -> Optional[DeviceInfo]:
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
                port.write(scpi_commands.disable_device_echo().get_bytes())
                time.sleep(0.1)
                port.write(scpi_commands.stop_streaming().get_bytes())
                time.sleep(0.1)
                port.write(scpi_commands.turn_device_on().get_bytes())
                time.sleep(0.1)
                port.write(scpi_commands.set_protobuf_stream_format().get_bytes())
                time.sleep(0.1)

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
        (useful for tests). When ``None`` a fresh :class:`UdpTransport`
        is opened on an ephemeral port and closed at exit.
        """
        if self._disposed:
            raise RuntimeError("WiFiDeviceFinder has been closed.")

        owns_transport = udp_transport is None
        # Track whether we actually opened the transport in THIS call,
        # so a failure during open() doesn't try to close an unopened
        # transport in finally — and so _fire_completed() always runs
        # (Qodo PR 17 pass 2 #3).
        opened_here = False

        # `time.monotonic()` is immune to wall-clock jumps — without
        # this a NTP step or DST transition mid-discover would either
        # extend the deadline forever or trip it instantly (Qodo PR
        # 17 pass 25 #4).
        deadline = time.monotonic() + max(timeout.total_seconds(), 0.0)
        seen_ips: set = set()
        discovered: List[DeviceInfo] = []
        with self._lock:
            try:
                if owns_transport:
                    udp_transport = UdpTransport(local_port=0)
                    # Mark opened_here BEFORE open() so the finally
                    # block still closes the transport if open()
                    # itself raises after partial socket creation
                    # (Qodo PR 17 pass 39 #3).
                    opened_here = True
                    udp_transport.open()

                # Broadcast both queries so devices on either firmware
                # variant respond.
                udp_transport.send_broadcast(self.DAQIFI_FINDER_QUERY, self._discovery_port)
                udp_transport.send_broadcast(self.NATIVE_FINDER_QUERY, self._discovery_port)

                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    result = udp_transport.receive(timeout=timedelta(seconds=min(0.5, remaining)))
                    if result is None:
                        continue
                    data, (ip, port) = result
                    if ip in seen_ips:
                        continue  # de-dup repeated responses from same device
                    text = data.decode("utf-8", errors="replace")
                    if _POWER_EVENT_MARKER in text:
                        # Discovery requests echo a power-event line; skip.
                        continue
                    seen_ips.add(ip)
                    info = DeviceInfo(
                        name=text.strip().splitlines()[0] if text.strip() else f"DAQiFi-{ip}",
                        serial_number="",
                        firmware_version="",
                        connection_type=ConnectionType.WIFI,
                        device_type=DeviceType.UNKNOWN,
                        ip_address=ip,
                        port=9760,  # firmware listens on the SCPI TCP port
                    )
                    discovered.append(info)
            finally:
                if opened_here:
                    try:
                        udp_transport.close()
                    except Exception:
                        pass
        # Fire callbacks OUTSIDE the lock — same deadlock concern
        # as the HID/Serial finders (Qodo PR 17 pass 25 #2).
        for info in discovered:
            self._fire_discovered(info)
        self._fire_completed()
        return discovered

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
