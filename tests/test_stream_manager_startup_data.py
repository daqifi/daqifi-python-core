"""#162 items 1, 5, 7: StreamManager keeps what arrives with START, maps
protobuf values by the device's enabled channels, and treats
analog_in_data_ts as an offset.

The board is an in-memory pyserial stand-in driven by the real
SerialInterface; on START it queues ``on_start`` bytes."""

import threading

import pytest

from daqifi._internal.comm import SerialInterface
from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
from daqifi.streaming import StreamManager


class _FakeSerial:
    def __init__(self, enabled_mask=1, on_start=b"", echo=False, start_delay_s=0.0):
        self.is_open = True
        self.rx = bytearray()
        self.lock = threading.Lock()
        self.writes = []
        self.enabled_mask = enabled_mask
        self.on_start = on_start
        self.echo = echo
        self.start_delay_s = start_delay_s

    def write(self, data):
        command = data.decode().strip()
        self.writes.append(command)
        with self.lock:
            if command == "ENABle:VOLTage:DC?":
                self.rx += f"{self.enabled_mask}\r\n".encode()
            elif command.startswith("SYSTem:ERRor?"):
                self.rx += b'0,"No error"\r\n'
            elif "STREAM:START" in command.upper():
                if self.echo:
                    self.rx += command.encode() + b"\r\n"
                if self.start_delay_s:
                    threading.Timer(self.start_delay_s, self._deliver_start).start()
                else:
                    self.rx += self.on_start
        return len(data)

    def _deliver_start(self):
        with self.lock:
            self.rx += self.on_start

    def flush(self):
        pass

    @property
    def in_waiting(self):
        with self.lock:
            return len(self.rx)

    def read(self, n):
        with self.lock:
            out = bytes(self.rx[:n])
            del self.rx[:n]
            return out

    def close(self):
        self.is_open = False


def _manager(fake, **kwargs):
    comm = SerialInterface("TEST-NOT-A-PORT", timeout=0.3)
    comm._serial = fake
    return StreamManager(comm, **kwargs)


def _frame(**fields) -> bytes:
    body = DaqifiOutMessage(**fields).SerializeToString()
    return bytes([len(body)]) + body


def test_the_csv_header_sent_with_start_is_kept() -> None:
    # Item 1: send_command(START) read and discarded the header, so the tick
    # rate and the column-to-channel mapping were lost for the session.
    header = (
        b"# Device: Nyquist NQ1\n# Timestamp Tick Rate: 1000 Hz\n"
        b"ain3_ts,ain3_val\n5000,2345\n"
    )
    stream = _manager(_FakeSerial(enabled_mask=0b1000, on_start=header), channels=[0], sample_rate=10, format=2)
    stream.start()
    sample = stream.read(1, timeout=1.0)[0]
    stream.stop()
    assert stream._tick_rate_hz == 1000
    assert sample.timestamp == pytest.approx(5.0)
    assert list(sample.channels) == [3]


def test_protobuf_frames_sent_with_start_are_kept() -> None:
    frames = b"".join(_frame(msg_time_stamp=1000 + i, analog_in_data_float=[1.5]) for i in range(3))
    stream = _manager(_FakeSerial(enabled_mask=1, on_start=frames), channels=[0], sample_rate=10, format=0)
    stream.start()
    samples = stream.read(3, timeout=1.0)
    stream.stop()
    assert [s.channels for s in samples] == [{0: 1.5}] * 3


def test_a_start_rejection_is_still_detected() -> None:
    from daqifi.exceptions import CommandError

    fake = _FakeSerial(enabled_mask=1, on_start=b'**ERROR: -113,"Undefined header"\r\n')
    stream = _manager(fake, channels=[0], sample_rate=10, format=2)
    with pytest.raises(CommandError, match="rejected"):
        stream.start()


def test_protobuf_values_follow_the_device_channels_not_the_requested_list() -> None:
    # Item 5: device mask 0b11, caller asked for [0]; each [1.0, 2.0] frame
    # came out as {0: 1.0} then {0: 2.0}, relabelling ch1 and doubling the rate.
    frames = _frame(msg_time_stamp=1, analog_in_data_float=[1.0, 2.0]) * 2
    stream = _manager(_FakeSerial(enabled_mask=0b11, on_start=frames), channels=[0], sample_rate=10, format=0)
    stream.start()
    samples = stream.read(2, timeout=1.0)
    stream.stop()
    assert [s.channels for s in samples] == [{0: 1.0}, {0: 1.0}]


def test_analog_in_data_ts_is_an_offset_to_msg_time_stamp() -> None:
    # Item 7: DaqifiOutMessage.proto field 4 is added to msg_time_stamp.
    stream = _manager(_FakeSerial(), channels=[0], sample_rate=10, format=0)
    stream._device_channels = [0]
    stream._tick_rate_hz = 1000
    samples = stream._samples_from_pb_message(
        DaqifiOutMessage(msg_time_stamp=100000, analog_in_data_float=[1.0], analog_in_data_ts=[5])
    )
    assert samples[0].timestamp == pytest.approx(100.005)


# Audit round 1 on #180.

def test_a_start_rejection_arriving_after_the_settle_delay_is_detected() -> None:
    from daqifi.exceptions import CommandError

    fake = _FakeSerial(on_start=b'**ERROR: -113,"Undefined header"\r\n', start_delay_s=0.25)
    stream = _manager(fake, channels=[0], sample_rate=10, format=2)
    with pytest.raises(CommandError, match="rejected"):
        stream.start()


def test_an_echo_terminator_does_not_take_a_frame_length_byte_of_0x0a() -> None:
    frame = _frame(msg_time_stamp=16384, analog_in_data_float=[1.5])
    assert frame[0] == 0x0A
    fake = _FakeSerial(enabled_mask=1, on_start=frame * 2, echo=True)
    stream = _manager(fake, channels=[0], sample_rate=10, format=0)
    stream.start()
    samples = stream.read(2, timeout=1.0)
    stream.stop()
    assert [s.channels for s in samples] == [{0: 1.5}, {0: 1.5}]


# Audit round 2 on #180.

def test_an_echo_before_a_partial_first_frame_keeps_the_frame() -> None:
    frame = _frame(msg_time_stamp=16384, analog_in_data_float=[1.5])
    fake = _FakeSerial(enabled_mask=1, on_start=frame[:-1], echo=True)
    stream = _manager(fake, channels=[0], sample_rate=10, format=0)
    stream.start()
    with fake.lock:
        fake.rx += frame[-1:] + frame
    samples = stream.read(2, timeout=1.0)
    stream.stop()
    assert [s.channels for s in samples] == [{0: 1.5}, {0: 1.5}]


@pytest.mark.parametrize("lead", [b"\r\n", b"DAQIFI>\r\n", b"DAQIFI> \r\n\r\n"])
def test_a_start_rejection_after_blank_or_prompt_lines_is_detected(lead) -> None:
    from daqifi.exceptions import CommandError

    fake = _FakeSerial(on_start=lead + b'**ERROR: -113,"Undefined header"\r\n')
    stream = _manager(fake, channels=[0], sample_rate=10, format=2)
    with pytest.raises(CommandError, match="rejected"):
        stream.start()


# Audit round 3 on #180.

@pytest.mark.parametrize("prompt", [b"DAQIFI> ", b"DAQIFI>\r\n"])
def test_a_prompt_before_the_first_frames_does_not_corrupt_them(prompt) -> None:
    frame = _frame(msg_time_stamp=16384, analog_in_data_float=[1.5])
    fake = _FakeSerial(enabled_mask=1, on_start=prompt + frame * 2)
    stream = _manager(fake, channels=[0], sample_rate=10, format=0)
    stream.start()
    samples = stream.read(2, timeout=1.0)
    stream.stop()
    assert [s.channels for s in samples] == [{0: 1.5}, {0: 1.5}]


# Audit round 4 on #180.

def test_a_prompt_split_across_the_start_read_does_not_corrupt_the_frames() -> None:
    frame = _frame(msg_time_stamp=16384, analog_in_data_float=[1.5])
    fake = _FakeSerial(enabled_mask=1, on_start=b"DAQIFI>")

    def late_tail():
        with fake.lock:
            fake.rx += b" " + frame * 2

    import threading as _th
    original_write = fake.write

    def write(data):
        n = original_write(data)
        if b"STReam:START" in data or b"STREAM:START" in data.upper():
            _th.Timer(0.3, late_tail).start()  # after the 200 ms settle + first read
        return n

    fake.write = write
    stream = _manager(fake, channels=[0], sample_rate=10, format=0)
    stream.start()
    samples = stream.read(2, timeout=1.0)
    stream.stop()
    assert [s.channels for s in samples] == [{0: 1.5}, {0: 1.5}]
