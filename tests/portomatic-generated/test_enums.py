"""Tests for `daqifi.enums` (port of WifiMode, WifiSecurityType, etc.)."""
from __future__ import annotations

from daqifi.enums import (
    FirmwareUpdateState,
    ProtobufMessageType,
    SdCardLogFormat,
    WifiMode,
    WifiSecurityType,
)


class TestWifiMode:
    def test_existing_network_value(self):
        assert WifiMode.EXISTING_NETWORK == 1

    def test_self_hosted_value(self):
        assert WifiMode.SELF_HOSTED == 4

    def test_int_compatible(self):
        assert int(WifiMode.SELF_HOSTED) == 4
        assert WifiMode(4) == WifiMode.SELF_HOSTED


class TestWifiSecurityType:
    def test_none_value(self):
        assert WifiSecurityType.NONE == 0

    def test_wpa_psk_value(self):
        assert WifiSecurityType.WPA_PSK_PHRASE == 3


class TestFirmwareUpdateState:
    def test_idle_is_zero(self):
        assert FirmwareUpdateState.IDLE == 0

    def test_lifecycle_progression_values(self):
        # Wire-protocol values must stay stable — tested explicitly.
        assert FirmwareUpdateState.IDLE == 0
        assert FirmwareUpdateState.PREPARING_DEVICE == 1
        assert FirmwareUpdateState.WAITING_FOR_BOOTLOADER == 2
        assert FirmwareUpdateState.CONNECTING == 3
        assert FirmwareUpdateState.ERASING_FLASH == 4
        assert FirmwareUpdateState.PROGRAMMING == 5
        assert FirmwareUpdateState.VERIFYING == 6
        assert FirmwareUpdateState.JUMPING_TO_APP == 7
        assert FirmwareUpdateState.COMPLETE == 8
        assert FirmwareUpdateState.FAILED == 9


class TestProtobufMessageType:
    def test_unknown_is_zero(self):
        assert ProtobufMessageType.UNKNOWN == 0

    def test_all_members_exist(self):
        # C# default-value enum: order matters for serialization.
        members = list(ProtobufMessageType)
        assert ProtobufMessageType.UNKNOWN in members
        assert ProtobufMessageType.STATUS in members
        assert ProtobufMessageType.STREAM in members
        assert ProtobufMessageType.SD_CARD in members
        assert ProtobufMessageType.ERROR in members


class TestSdCardLogFormat:
    def test_protobuf_default(self):
        assert SdCardLogFormat.PROTOBUF == 0

    def test_format_values(self):
        assert SdCardLogFormat.PROTOBUF == 0
        assert SdCardLogFormat.JSON == 1
        assert SdCardLogFormat.CSV == 2
