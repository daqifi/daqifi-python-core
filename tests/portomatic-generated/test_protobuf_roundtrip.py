"""Round-trip the generated DaqifiOutMessage through update_from_protobuf (closes #8).

The ticket called for the duck-typed bridge in ``DeviceMetadata.update_from_protobuf``
to work against a real, deserialized protobuf message — not just the test
doubles in ``test_device_metadata_protobuf.py``. This locks that contract
in: a known set of fields → serialize → wire bytes → deserialize → feed
through update_from_protobuf → metadata populates.

If the generated ``DaqifiOutMessage_pb2.py`` ever drifts from the
duck-typed contract (e.g. a renamed proto field, a changed type), this
test catches it before the metadata-pipeline tests do.
"""
from __future__ import annotations

import pytest

from daqifi._internal.comm import DeviceMetadata, DeviceType
from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage


pytestmark = pytest.mark.portomatic_generated


def test_serialized_protobuf_round_trip_populates_metadata() -> None:
    """Real protobuf class round-trip exercises every type the bridge handles."""
    src = DaqifiOutMessage()
    src.device_pn = "Nq1"
    src.device_sn = 1234567890123456789
    src.device_fw_rev = "3.4.6b1"
    src.device_hw_rev = "2.0.0"
    src.host_name = "DAQiFi-A7B4"
    src.ssid = "Tesla"
    src.device_port = 9760
    src.wifi_security_mode = 3
    src.wifi_inf_mode = 1
    src.ip_addr = bytes([192, 168, 1, 60])
    src.mac_addr = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])

    wire = src.SerializeToString()
    decoded = DaqifiOutMessage.FromString(wire)

    md = DeviceMetadata()
    md.update_from_protobuf(decoded)

    assert md.part_number == "Nq1"
    assert md.serial_number == "1234567890123456789"
    assert md.firmware_version == "3.4.6b1"
    assert md.hardware_revision == "2.0.0"
    assert md.host_name == "DAQiFi-A7B4"
    assert md.ssid == "Tesla"
    assert md.device_port == 9760
    assert md.wifi_security_mode == 3
    assert md.wifi_infrastructure_mode == 1
    assert md.ip_address == "192.168.1.60"
    assert md.mac_address == "AA-BB-CC-DD-EE-FF"
    assert md.device_type == DeviceType.NYQUIST_1


def test_empty_serialized_protobuf_roundtrip_leaves_defaults() -> None:
    """An empty protobuf shouldn't clobber existing metadata defaults."""
    src = DaqifiOutMessage()
    wire = src.SerializeToString()
    decoded = DaqifiOutMessage.FromString(wire)

    md = DeviceMetadata()
    md.update_from_protobuf(decoded)

    # All defaults preserved — no field on the proto was set.
    assert md.part_number == ""
    assert md.serial_number == ""
    assert md.firmware_version == ""
    assert md.device_type == DeviceType.UNKNOWN


def test_partial_update_does_not_clobber_existing_fields() -> None:
    """Subsequent updates with empty fields preserve previously populated values."""
    md = DeviceMetadata()

    first = DaqifiOutMessage()
    first.device_pn = "Nq1"
    first.device_sn = 12345
    first.device_fw_rev = "3.4.6b1"
    md.update_from_protobuf(DaqifiOutMessage.FromString(first.SerializeToString()))

    # Second update: only ssid populated, rest default — should not erase prior values.
    second = DaqifiOutMessage()
    second.ssid = "Tesla"
    md.update_from_protobuf(DaqifiOutMessage.FromString(second.SerializeToString()))

    assert md.part_number == "Nq1"
    assert md.serial_number == "12345"
    assert md.firmware_version == "3.4.6b1"
    assert md.ssid == "Tesla"


def test_proto_field_inventory_matches_bridge_contract() -> None:
    """Lock the wire protocol: fields update_from_protobuf reads from the proto."""
    proto_field_names = {f.name for f in DaqifiOutMessage.DESCRIPTOR.fields}
    expected = {
        "device_pn", "device_sn", "device_fw_rev", "device_hw_rev",
        "ssid", "host_name", "device_port", "wifi_security_mode",
        "wifi_inf_mode", "ip_addr", "mac_addr",
    }
    missing = expected - proto_field_names
    assert not missing, f"protobuf is missing bridge-contract fields: {missing}"
