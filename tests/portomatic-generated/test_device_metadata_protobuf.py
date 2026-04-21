"""Tests for DeviceMetadata.update_from_protobuf and detect_device_type_from_part_number.

Closes the divergence noted in the original DeviceMetadata docstring —
NetworkAddressHelper and DeviceTypeDetector are now ported, so the
protobuf-bridge method is wired up.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from daqifi._internal.comm import (
    DeviceCapabilities,
    DeviceMetadata,
    DeviceType,
    detect_device_type_from_part_number,
)


@dataclass
class FakeProtoMessage:
    """Duck-typed stand-in for the protobuf DaqifiOutMessage."""

    device_pn: str = ""
    device_sn: int = 0
    device_fw_rev: str = ""
    device_hw_rev: str = ""
    ssid: str = ""
    host_name: str = ""
    device_port: int = 0
    wifi_security_mode: int = 0
    wifi_inf_mode: int = 0
    ip_addr: bytes = b""
    mac_addr: bytes = b""
    net_mask: bytes = b""
    gateway: bytes = b""
    primary_dns: bytes = b""
    secondary_dns: bytes = b""
    analog_in_port_num: int = 0
    analog_out_port_num: int = 0
    digital_port_num: int = 0


# -- detect_device_type_from_part_number ---------------------------------

class TestDetectDeviceType:
    @pytest.mark.parametrize("pn,expected", [
        ("Nq1", DeviceType.NYQUIST_1),
        ("nq1", DeviceType.NYQUIST_1),
        ("NQ1", DeviceType.NYQUIST_1),
        ("Nq2", DeviceType.NYQUIST_2),
        ("Nq3", DeviceType.NYQUIST_3),
    ])
    def test_known_part_numbers(self, pn, expected):
        assert detect_device_type_from_part_number(pn) == expected

    @pytest.mark.parametrize("pn", [None, "", "  ", "unknown", "Foo123"])
    def test_unknown_returns_unknown(self, pn):
        assert detect_device_type_from_part_number(pn) == DeviceType.UNKNOWN

    def test_strips_whitespace(self):
        assert detect_device_type_from_part_number("  Nq2  ") == DeviceType.NYQUIST_2


# -- update_from_protobuf -----------------------------------------------

class TestUpdateFromProtobuf:
    def test_none_message_raises(self):
        m = DeviceMetadata()
        with pytest.raises(TypeError):
            m.update_from_protobuf(None)

    def test_part_number_sets_device_type_and_capabilities(self):
        m = DeviceMetadata()
        proto = FakeProtoMessage(device_pn="Nq2")
        m.update_from_protobuf(proto)
        assert m.part_number == "Nq2"
        assert m.device_type == DeviceType.NYQUIST_2
        # Capabilities derived from device type — should not be the default.
        assert m.capabilities.has_wifi is True
        assert m.capabilities.has_sd_card is True

    def test_blank_part_number_does_not_overwrite(self):
        m = DeviceMetadata(part_number="existing")
        proto = FakeProtoMessage(device_pn="")
        m.update_from_protobuf(proto)
        assert m.part_number == "existing"

    def test_serial_number_zero_does_not_set(self):
        m = DeviceMetadata(serial_number="existing")
        proto = FakeProtoMessage(device_sn=0)
        m.update_from_protobuf(proto)
        assert m.serial_number == "existing"

    def test_serial_number_nonzero_sets_as_string(self):
        m = DeviceMetadata()
        proto = FakeProtoMessage(device_sn=12345)
        m.update_from_protobuf(proto)
        assert m.serial_number == "12345"

    def test_firmware_and_hardware_revisions(self):
        m = DeviceMetadata()
        proto = FakeProtoMessage(device_fw_rev="1.2.3", device_hw_rev="A.5")
        m.update_from_protobuf(proto)
        assert m.firmware_version == "1.2.3"
        assert m.hardware_revision == "A.5"

    def test_ssid_and_hostname(self):
        m = DeviceMetadata()
        proto = FakeProtoMessage(ssid="MyWiFi", host_name="DAQ-001")
        m.update_from_protobuf(proto)
        assert m.ssid == "MyWiFi"
        assert m.host_name == "DAQ-001"

    def test_device_port(self):
        m = DeviceMetadata()
        proto = FakeProtoMessage(device_port=5000)
        m.update_from_protobuf(proto)
        assert m.device_port == 5000

    def test_zero_device_port_does_not_overwrite(self):
        m = DeviceMetadata(device_port=8080)
        proto = FakeProtoMessage(device_port=0)
        m.update_from_protobuf(proto)
        assert m.device_port == 8080

    def test_wifi_modes_only_set_when_positive(self):
        m = DeviceMetadata(wifi_security_mode=99, wifi_infrastructure_mode=88)
        proto = FakeProtoMessage(wifi_security_mode=0, wifi_inf_mode=0)
        m.update_from_protobuf(proto)
        assert m.wifi_security_mode == 99
        assert m.wifi_infrastructure_mode == 88

    def test_wifi_modes_set_when_positive(self):
        m = DeviceMetadata()
        proto = FakeProtoMessage(wifi_security_mode=3, wifi_inf_mode=1)
        m.update_from_protobuf(proto)
        assert m.wifi_security_mode == 3
        assert m.wifi_infrastructure_mode == 1

    def test_ip_address_decoded(self):
        m = DeviceMetadata()
        proto = FakeProtoMessage(ip_addr=b"\xc0\xa8\x01\x64")
        m.update_from_protobuf(proto)
        assert m.ip_address == "192.168.1.100"

    def test_mac_address_decoded(self):
        m = DeviceMetadata()
        proto = FakeProtoMessage(mac_addr=b"\xaa\xbb\xcc\xdd\xee\xff")
        m.update_from_protobuf(proto)
        assert m.mac_address == "AA-BB-CC-DD-EE-FF"

    def test_empty_ip_does_not_overwrite(self):
        m = DeviceMetadata(ip_address="10.0.0.1")
        proto = FakeProtoMessage(ip_addr=b"")
        m.update_from_protobuf(proto)
        assert m.ip_address == "10.0.0.1"

    def test_channel_counts(self):
        m = DeviceMetadata()
        # Set part number first so capabilities aren't the default-empty
        proto = FakeProtoMessage(
            device_pn="Nq1",
            analog_in_port_num=8,
            analog_out_port_num=2,
            digital_port_num=16,
        )
        m.update_from_protobuf(proto)
        assert m.capabilities.analog_input_channels == 8
        assert m.capabilities.analog_output_channels == 2
        assert m.capabilities.digital_channels == 16

    def test_zero_channel_counts_do_not_overwrite(self):
        m = DeviceMetadata()
        m.capabilities = DeviceCapabilities(analog_input_channels=4)
        proto = FakeProtoMessage(analog_in_port_num=0)
        m.update_from_protobuf(proto)
        assert m.capabilities.analog_input_channels == 4

    def test_full_message_round_trip(self):
        m = DeviceMetadata()
        proto = FakeProtoMessage(
            device_pn="Nq3",
            device_sn=42,
            device_fw_rev="1.0.0",
            device_hw_rev="B.1",
            ssid="HomeNet",
            host_name="daqifi-floor",
            device_port=9760,
            wifi_security_mode=3,
            wifi_inf_mode=1,
            ip_addr=b"\x0a\x00\x00\x05",
            mac_addr=b"\x00\x11\x22\x33\x44\x55",
            analog_in_port_num=8,
            analog_out_port_num=0,
            digital_port_num=16,
        )
        m.update_from_protobuf(proto)
        assert m.part_number == "Nq3"
        assert m.device_type == DeviceType.NYQUIST_3
        assert m.serial_number == "42"
        assert m.firmware_version == "1.0.0"
        assert m.hardware_revision == "B.1"
        assert m.ssid == "HomeNet"
        assert m.host_name == "daqifi-floor"
        assert m.device_port == 9760
        assert m.wifi_security_mode == 3
        assert m.wifi_infrastructure_mode == 1
        assert m.ip_address == "10.0.0.5"
        assert m.mac_address == "00-11-22-33-44-55"
        assert m.capabilities.analog_input_channels == 8
        assert m.capabilities.digital_channels == 16
