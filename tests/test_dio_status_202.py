"""#202: DIO and channel state from the status message.

1. The streaming device reported every DIO pin as Input: protobuf field 37
   (``digital_port_dir``, bit n set = DIO n is an input; the firmware's
   ``IsInput``, written little-endian by ``int2PBByteArray``) was ignored
   (upstream 5449896).
2. ``AnalogChannel.is_enabled`` was always False: field 22
   (``analog_in_port_enabled``, bit n set = AI n enabled) was never read
   (upstream 405eeac).
4. The legacy DIO direction and state setters read the port mask, changed
   one bit and wrote it back with no lock, so two pins set at once lost
   one update (#163 item 12).
"""

import threading
import time

import pytest

from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi._internal.scpi import ChannelDirection
from daqifi.devices import AnalogChannel, DaqifiStreamingDevice, DigitalChannel


def _populate(**fields):
    device = DaqifiStreamingDevice(name="d", transport=None)
    device.populate_channels_from_status(DaqifiOutMessage(**fields))
    return device


def _digital(device):
    return [c for c in device.channels if isinstance(c, DigitalChannel)]


def _analog(device):
    return [c for c in device.channels if isinstance(c, AnalogChannel)]


# ---- 1: DIO direction ---------------------------------------------------


def test_dio_direction_follows_the_reported_mask() -> None:
    device = _populate(digital_port_num=4, digital_port_dir=bytes([0b0101]))
    assert [c.direction for c in _digital(device)] == [
        ChannelDirection.Input, ChannelDirection.Output,
        ChannelDirection.Input, ChannelDirection.Output,
    ]


def test_dio_direction_reads_the_mask_little_endian_across_bytes() -> None:
    # 16 pins: every bit set except DIO9 (byte 1, bit 1).
    device = _populate(digital_port_num=16, digital_port_dir=bytes([0xFF, 0xFD]))
    directions = [c.direction for c in _digital(device)]
    assert directions[9] == ChannelDirection.Output
    assert all(d == ChannelDirection.Input for i, d in enumerate(directions) if i != 9)


def test_dio_direction_without_a_mask_stays_input() -> None:
    device = _populate(digital_port_num=3)
    assert [c.direction for c in _digital(device)] == [ChannelDirection.Input] * 3


def test_a_pin_past_a_short_mask_stays_input() -> None:
    # One byte reported for 10 pins: DIO8 and DIO9 aren't covered.
    device = _populate(digital_port_num=10, digital_port_dir=bytes([0x00]))
    directions = [c.direction for c in _digital(device)]
    assert directions[:8] == [ChannelDirection.Output] * 8
    assert directions[8:] == [ChannelDirection.Input] * 2


# ---- 2: analog is_enabled -------------------------------------------------


def test_analog_is_enabled_follows_the_reported_mask() -> None:
    device = _populate(analog_in_port_num=4, analog_in_port_enabled=bytes([0b1010]))
    assert [c.is_enabled for c in _analog(device)] == [False, True, False, True]


def test_analog_is_enabled_reads_the_mask_little_endian_across_bytes() -> None:
    device = _populate(analog_in_port_num=16, analog_in_port_enabled=bytes([0x00, 0x10]))
    enabled = [c.is_enabled for c in _analog(device)]
    assert enabled[12] is True
    assert sum(enabled) == 1


def test_analog_without_an_enable_mask_is_disabled() -> None:
    device = _populate(analog_in_port_num=2)
    assert [c.is_enabled for c in _analog(device)] == [False, False]


# ---- 4: DIO setters are one unit per device -----------------------------


class _PortDevice:
    """A comm whose DIO port registers answer after a delay, so two
    setters' read-modify-writes overlap unless they're serialized."""

    def __init__(self, direction=0, state=0):
        self.registers = {"DIR": direction, "STATe": state}
        self._lock = threading.Lock()

    def _register(self, command):
        return "DIR" if "DIRection" in command else "STATe"

    def send_query(self, command):
        with self._lock:
            value = self.registers[self._register(command)]
        time.sleep(0.05)
        return str(value)

    def send_set_with_error(self, command, value):
        with self._lock:
            self.registers[self._register(command)] = int(value)
        return True, ""


def _set_together(setters):
    barrier = threading.Barrier(len(setters))
    errors = []

    def _run(setter):
        barrier.wait()
        try:
            setter()
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=_run, args=(s,)) for s in setters]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errors, errors


def test_two_pins_set_to_output_at_once_both_stick() -> None:
    from daqifi.channels import DigitalIOChannel

    comm = _PortDevice()
    pins = [DigitalIOChannel(i, comm) for i in (0, 1)]
    _set_together([lambda p=p: setattr(p, "direction", "output") for p in pins])
    assert comm.registers["DIR"] == 0b11


def test_two_pins_driven_high_at_once_both_stick() -> None:
    from daqifi.channels import DigitalIOChannel

    comm = _PortDevice(direction=0b11)
    pins = [DigitalIOChannel(i, comm) for i in (0, 1)]
    _set_together([lambda p=p: setattr(p, "state", True) for p in pins])
    assert comm.registers["STATe"] == 0b11
