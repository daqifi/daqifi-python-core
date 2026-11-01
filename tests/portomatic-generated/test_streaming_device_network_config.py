"""Tests for #33 — DaqifiStreamingDevice as a concrete impl of the
NetworkConfigurable Protocol.

The orchestration methods (`update_network_configuration`,
`prepare_lan_interface`, `prepare_sd_interface`) are implemented on
main but lack end-to-end tests proving the canonical .NET sequence.
This file fills that gap.
"""
from __future__ import annotations

import threading
from typing import List, Optional

import pytest

from daqifi.devices import DaqifiStreamingDevice
from daqifi.enums import WifiMode, WifiSecurityType
from daqifi.network import NetworkConfigurable, NetworkConfiguration


pytestmark = pytest.mark.portomatic_generated


# ---- Fakes (mirrors test_sd_card_operations_impl.py) ----------------------


class _ScriptedSerialStream:
    def __init__(self, response_map: Optional[dict] = None) -> None:
        self.written: List[bytes] = []
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.response_map = dict(response_map or {})

    def write(self, data: bytes) -> int:
        b = bytes(data)
        self.written.append(b)
        for trigger, response in self.response_map.items():
            if trigger in b:
                self.feed(response)
        return len(b)

    def flush(self) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def read(self, n: int) -> bytes:
        with self._lock:
            take = bytes(self._buf[:n])
            del self._buf[:n]
            return take

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)


class _ScriptedTransport:
    def __init__(self, response_map: Optional[dict] = None) -> None:
        self.stream = _ScriptedSerialStream(response_map)
        self._connected = False
        self.status_handlers: list = []

    def add_status_changed(self, h):
        self.status_handlers.append(h)

    def remove_status_changed(self, h):
        if h in self.status_handlers:
            self.status_handlers.remove(h)

    def connect(self, retry_options=None):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def close(self):
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


def _make_device() -> DaqifiStreamingDevice:
    transport = _ScriptedTransport()
    sd = DaqifiStreamingDevice(name="net-test", transport=transport)
    sd.connect()
    # Override the WiFi-restart sleep so tests don't pay 2s per call.
    sd._WIFI_MODULE_RESTART_DELAY_S = 0.0  # type: ignore[attr-defined]
    # Also zero the SD settle delay — prepare_sd_interface() sleeps
    # twice via this constant, and tests only assert command ordering.
    sd._SD_INTERFACE_SETTLE_DELAY_S = 0.0  # type: ignore[attr-defined]
    return sd


# ---- Protocol structural conformance --------------------------------------


def test_streaming_device_satisfies_network_configurable_protocol() -> None:
    sd = DaqifiStreamingDevice(name="x", transport=None)
    assert isinstance(sd, NetworkConfigurable)


# ---- update_network_configuration -----------------------------------------


class TestUpdateNetworkConfiguration:
    def test_requires_connection(self) -> None:
        sd = DaqifiStreamingDevice(name="x", transport=None)
        with pytest.raises(RuntimeError, match="not connected"):
            sd.update_network_configuration(NetworkConfiguration(ssid="x"))

    def test_rejects_none(self) -> None:
        sd = _make_device()
        try:
            with pytest.raises(TypeError, match="must not be None"):
                sd.update_network_configuration(None)  # type: ignore[arg-type]
        finally:
            sd.close()

    def test_rejects_empty_ssid(self) -> None:
        sd = _make_device()
        try:
            with pytest.raises(ValueError, match="SSID"):
                sd.update_network_configuration(NetworkConfiguration(ssid=""))
        finally:
            sd.close()

    def test_rejects_wpa_without_password(self) -> None:
        sd = _make_device()
        try:
            with pytest.raises(ValueError, match="password"):
                sd.update_network_configuration(
                    NetworkConfiguration(
                        ssid="x",
                        security_type=WifiSecurityType.WPA_PSK_PHRASE,
                        password="",
                    )
                )
        finally:
            sd.close()

    def test_existing_network_emits_canonical_sequence(self) -> None:
        sd = _make_device()
        try:
            cfg = NetworkConfiguration(
                ssid="MyAP",
                mode=WifiMode.EXISTING_NETWORK,
                security_type=WifiSecurityType.WPA_PSK_PHRASE,
                password="hunter2",
            )
            sd.update_network_configuration(cfg)
            written = b"".join(sd._transport.stream.written)

            # Stage, then LAN:APPLY 1 last (apply, and save only if the
            # apply is accepted; diverges from daqifi-core#391's SAVE-first
            # on purpose, see update_network_configuration) — verify ordering.
            mode_idx = written.find(b"NETType 1")
            ssid_idx = written.find(b"LAN:SSID")
            sec_idx = written.find(b"SECurity 3")  # WPA = 3
            pass_idx = written.find(b"LAN:PASs")
            apply_idx = written.find(b"LAN:APPLY")
            disable_sd_idx = written.find(b"SD:ENAble 0")
            enable_lan_idx = written.find(b"LAN:ENAbled 1")
            save_idx = written.find(b"LAN:SAVE")
            apply_save_idx = written.find(b"LAN:APPLY 1")

            assert mode_idx >= 0
            assert ssid_idx > mode_idx, "SSID must come after mode"
            assert sec_idx > ssid_idx, "Security must come after SSID"
            assert pass_idx > sec_idx, "Password must come after security"
            assert disable_sd_idx > pass_idx, (
                "prepare_lan_interface stages after the credentials"
            )
            assert enable_lan_idx > disable_sd_idx, (
                "EnableNetworkLan must come after DisableStorageSd"
            )
            assert save_idx == -1, "No separate save: APPLY 1 saves"
            assert apply_save_idx > enable_lan_idx, (
                "APPLY 1 comes last, after prepare_lan"
            )
            assert written.rstrip().endswith(b"LAN:APPLY 1"), "Nothing after apply"
        finally:
            sd.close()

    def test_self_hosted_uses_different_nettype(self) -> None:
        sd = _make_device()
        try:
            cfg = NetworkConfiguration(
                ssid="MyAP",
                mode=WifiMode.SELF_HOSTED,
                security_type=WifiSecurityType.NONE,
            )
            sd.update_network_configuration(cfg)
            written = b"".join(sd._transport.stream.written)
            # Self-hosted = NETType 4 (AP), security 0 (open).
            assert b"NETType 4" in written
            assert b"SECurity 0" in written
            # No password command for open networks.
            assert b"LAN:PASs" not in written
        finally:
            sd.close()

    def test_open_security_skips_password(self) -> None:
        sd = _make_device()
        try:
            cfg = NetworkConfiguration(
                ssid="OpenAP",
                mode=WifiMode.EXISTING_NETWORK,
                security_type=WifiSecurityType.NONE,
            )
            sd.update_network_configuration(cfg)
            written = b"".join(sd._transport.stream.written)
            assert b"SECurity 0" in written
            assert b"LAN:PASs" not in written
        finally:
            sd.close()

    def test_updates_local_snapshot_after_success(self) -> None:
        sd = _make_device()
        try:
            cfg = NetworkConfiguration(
                ssid="cached",
                mode=WifiMode.EXISTING_NETWORK,
                security_type=WifiSecurityType.WPA_PSK_PHRASE,
                password="x",
            )
            sd.update_network_configuration(cfg)
            snap = sd.network_configuration
            assert snap.ssid == "cached"
            assert snap.mode == WifiMode.EXISTING_NETWORK
            assert snap.security_type == WifiSecurityType.WPA_PSK_PHRASE
        finally:
            sd.close()

    def test_stops_streaming_before_apply(self) -> None:
        sd = _make_device()
        try:
            sd._is_streaming = True
            cfg = NetworkConfiguration(
                ssid="x",
                mode=WifiMode.EXISTING_NETWORK,
                security_type=WifiSecurityType.NONE,
            )
            sd.update_network_configuration(cfg)
            written = b"".join(sd._transport.stream.written)
            # STReam:STOP must precede the WiFi config commands.
            stop_idx = written.find(b"STReam:STOP")
            apply_idx = written.find(b"LAN:APPLY")
            assert stop_idx >= 0
            assert apply_idx > stop_idx
            assert sd.is_streaming is False
        finally:
            sd.close()


# ---- prepare_lan_interface / prepare_sd_interface --------------------------


class TestPrepareLanInterface:
    def test_emits_disable_sd_then_enable_lan(self) -> None:
        sd = _make_device()
        try:
            sd._transport.stream.written.clear()
            sd.prepare_lan_interface()
            written = b"".join(sd._transport.stream.written)
            sd_off = written.find(b"SD:ENAble 0")
            lan_on = written.find(b"LAN:ENAbled 1")
            assert sd_off >= 0
            assert lan_on > sd_off, "Enable LAN must come after Disable SD"
        finally:
            sd.close()

    def test_clears_local_logging_flag(self) -> None:
        sd = _make_device()
        try:
            sd._is_logging_to_sd = True
            sd.prepare_lan_interface()
            assert sd._is_logging_to_sd is False
        finally:
            sd.close()


class TestPrepareSdInterface:
    def test_emits_disable_lan_then_enable_sd(self) -> None:
        sd = _make_device()
        try:
            sd._transport.stream.written.clear()
            sd.prepare_sd_interface()
            written = b"".join(sd._transport.stream.written)
            lan_off = written.find(b"LAN:ENAbled 0")
            sd_on = written.find(b"SD:ENAble 1")
            assert lan_off >= 0
            assert sd_on > lan_off, "Enable SD must come after Disable LAN"
        finally:
            sd.close()
