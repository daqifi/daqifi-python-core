"""
Data streaming for DAQiFi Nyquist devices.

Provides real-time data acquisition with support for multiple channels,
configurable sample rates, and data export.
"""

import logging
import math
import time
import threading
from collections import deque
from typing import List, Dict, Optional, Callable, Any, Deque, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ._internal.comm import SerialInterface
from ._internal import scpi
from .communication import ProtobufMessageParser
from .exceptions import CommandError, ConfigurationError

_LOGGER = logging.getLogger(__name__)

# A poll that took a complete record which gave no sample (see
# StreamManager._poll_sample): not idle, so no wait before the next poll.
_SKIPPED = object()

# Samples a StreamManager keeps in its buffer by default (about 100 s at
# 1 kHz): enough to export a recent window, bounded so a long-running stream
# can't exhaust memory. Pass buffer_size=None for an unbounded buffer.
DEFAULT_BUFFER_SIZE = 100_000


class StreamState(Enum):
    """Streaming state"""
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"



# How long a reader waits when a poll found nothing to deliver. Short enough
# to keep up with the firmware's fastest rates; a reader never waits while
# samples are waiting (#192).
_IDLE_POLL_SECONDS = 0.001

# The device's timestamp tick counter is an unsigned 32-bit value.
_TICK_RANGE = 1 << 32
_TICK_HALF_RANGE = 1 << 31
_TICK_MASK = _TICK_RANGE - 1

@dataclass
class Sample:
    """Single sample across all enabled channels.

    ``timestamp`` is **device-relative seconds** in the common decode
    paths — the JSON / CSV parsers compute it as ``ts / tick_hz`` from
    the firmware's tick counter. It is NOT UNIX epoch seconds; feeding
    it directly to ``datetime.fromtimestamp`` yields 1970-era values.
    Only the fallback paths (no ``tick_hz`` known yet, or the protobuf
    decoder) populate it with ``time.time()``. The per-channel dispatch
    pipeline anchors it against wall-clock at first sample to recover a
    real epoch — see :meth:`StreamManager._dispatch_per_channel`. If
    you need a wall-clock time from a Sample directly, use the per-
    channel ``DataSample.timestamp`` instead.
    """
    timestamp: float
    channels: Dict[int, float]  # Channel ID -> voltage
    # How a JSON/CSV record's values were read (the firmware's rule, as for
    # SD logs): "volts" (written with decimals, the firmware's default),
    # "millivolts" or "raw" (integers, as integer_units said), or
    # "raw-assumed" (integers read as raw ADC codes because nobody said;
    # that case warns). Raw codes are unscaled: the live stream carries no
    # calibration. None for protobuf samples.
    units: Optional[str] = None

    def __getitem__(self, channel: int) -> float:
        """Get channel value by index; NaN when this sample has no value for
        the channel (it wasn't in the record), never a fake 0 V."""
        return self.channels.get(channel, math.nan)

    def __repr__(self) -> str:
        ch_str = ", ".join(f"{ch}:{val:.3f}" for ch, val in sorted(self.channels.items()))
        return f"Sample(t={self.timestamp:.3f}, {ch_str})"


@dataclass
class SampleBuffer:
    """Buffer of samples.

    ``max_samples`` bounds it: once it holds more than ``max_samples`` plus a
    tenth again, the oldest samples are dropped down to ``max_samples`` (in
    chunks, so adding stays cheap). None keeps every sample.

    A streaming thread may add samples while another thread exports them.
    Adding and clearing hold the buffer's lock, and every export works on one
    snapshot of the samples taken under it, so a trim during an export can't
    pair a value with another sample's timestamp.
    """
    samples: List[Sample] = field(default_factory=list)
    channels: List[int] = field(default_factory=list)
    sample_rate: float = 0.0
    start_time: Optional[float] = None
    max_samples: Optional[int] = None

    def __post_init__(self) -> None:
        # Not a dataclass field: asdict(), deepcopy and pickle must not
        # reach it (a lock can't be copied); each copy gets its own.
        self._lock = threading.Lock()

    def __getstate__(self) -> Dict[str, Any]:
        state = dict(self.__dict__)
        state.pop("_lock", None)
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.Lock()

    def add_sample(self, sample: Sample):
        """Add sample to buffer"""
        with self._lock:
            if self.start_time is None:
                self.start_time = sample.timestamp
            self.samples.append(sample)
            if self.max_samples is not None:
                excess = len(self.samples) - self.max_samples
                if excess > max(1, self.max_samples // 10):
                    del self.samples[:excess]

    def clear(self):
        """Clear buffer"""
        with self._lock:
            self.samples.clear()
            self.start_time = None

    def snapshot(self) -> "Tuple[Optional[float], List[Sample]]":
        """``(start_time, samples)`` as of one moment: a copy taken under
        the buffer's lock, safe to iterate while samples are added."""
        with self._lock:
            return self.start_time, list(self.samples)

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> Sample:
        return self.samples[index]

    def __iter__(self):
        return iter(self.snapshot()[1])

    def get_channel_data(self, channel: int) -> List[float]:
        """
        Get all data for a specific channel.

        Args:
            channel: Channel index

        Returns:
            List of voltage values (NaN where a sample has no value for it)
        """
        return [sample[channel] for sample in self.snapshot()[1]]

    def get_timestamps(self) -> List[float]:
        """
        Get all timestamps.

        Returns:
            List of timestamps (seconds since start)
        """
        start, samples = self.snapshot()
        return _relative_timestamps(start, samples)

    def to_dict(self) -> Dict[str, Any]:
        """
        Export to dictionary.

        Returns:
            Dictionary with timestamps and channel data (NaN where a sample
            has no value for a channel)
        """
        start, samples = self.snapshot()
        result = {
            'timestamps': _relative_timestamps(start, samples),
            'sample_rate': self.sample_rate,
            'num_samples': len(samples),
        }

        for ch in self.channels:
            result[f'channel_{ch}'] = [sample[ch] for sample in samples]

        return result

    def to_csv(self, filename: str, include_header: bool = True):
        """
        Export to CSV file.

        Args:
            filename: Output filename
            include_header: Include column headers

        Example:
            buffer.to_csv("data.csv")
        """
        import csv

        start, samples = self.snapshot()
        with open(filename, 'w', newline='') as f:
            writer = csv.writer(f)

            # Write header
            if include_header:
                header = ['timestamp'] + [f'channel_{ch}' for ch in sorted(self.channels)]
                writer.writerow(header)

            # Write data: each row from one sample of the same snapshot
            for ts, sample in zip(_relative_timestamps(start, samples), samples):
                row = [ts]
                for ch in sorted(self.channels):
                    row.append(sample[ch])
                writer.writerow(row)

    def to_json(self, filename: str):
        """
        Export to JSON file.

        Args:
            filename: Output filename

        Example:
            buffer.to_json("data.json")
        """
        import json

        # A missing value is NaN for numeric use, but bare NaN isn't JSON:
        # write it as null, and refuse any other non-finite value.
        data = {
            key: ([None if isinstance(v, float) and math.isnan(v) else v for v in value]
                  if isinstance(value, list) else value)
            for key, value in self.to_dict().items()
        }
        with open(filename, 'w') as f:
            json.dump(data, f, indent=2, allow_nan=False)

    def to_dataframe(self):
        """
        Export to pandas DataFrame.

        Returns:
            pandas.DataFrame with timestamps and channel data

        Raises:
            ImportError: If pandas not installed

        Example:
            df = buffer.to_dataframe()
            df.plot()
        """
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("pandas is required for DataFrame export. Install with: pip install pandas")

        data = self.to_dict()
        return pd.DataFrame(data)


def _relative_timestamps(start: Optional[float], samples: List[Sample]) -> List[float]:
    """Seconds since ``start`` for each of ``samples`` (empty without one)."""
    if start is None:
        return []
    return [s.timestamp - start for s in samples]


# Prompt forms that may lead the START reply, longest first so a prompt line
# is taken with its CR LF as a unit: the firmware's "DAQIFI> " (microrl
# _PROMPT_DEFAULT), and the same on a line of its own.
_CONSOLE_PROMPTS = (b"DAQIFI> \r\n", b"DAQIFI>\r\n", b"DAQIFI> ", b"DAQIFI>")


_LIVE_INTEGER_UNITS_WARNING = (
    "Live stream analog values are integers, read as raw ADC codes "
    "(unscaled). If the device streams at voltage precision 0 they are "
    "millivolts: pass integer_units='millivolts' to the stream, or "
    "integer_units='raw' to confirm and silence this "
    "(daqifi-nyquist-firmware#1179)."
)


def _csv_value(text: str):
    """A CSV value as written: int for an integer, float for a decimal."""
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError(f"non-finite CSV value {text!r}")
        return value


class StreamManager:
    """
    Manages data streaming from Nyquist device.

    Provides blocking and callback-based streaming with configurable
    sample rates and channel selection.
    """

    # Maximum size of the protobuf decode buffer before forced trim.
    # 1 MiB is ~200x the largest realistic single PB streaming frame
    # (well under 1 KB on observed firmware); past this point the
    # wire is desynced, not just slow. Class-level constant so tests
    # can monkey-patch without crafting MiB-sized fixtures.
    _PB_BUFFER_HARD_CAP = 1024 * 1024

    def __init__(
        self,
        comm: SerialInterface,
        channels: List[int],
        sample_rate: int = 1000,
        format: int = 1,
        per_channel_dispatch: Optional[Callable[[int, float, float], None]] = None,
        buffer_size: Optional[int] = DEFAULT_BUFFER_SIZE,
        integer_units: Optional[str] = None,
    ):
        """
        Initialize stream manager.

        Args:
            comm: Serial communication interface
            channels: List of channel indices to stream
            sample_rate: Sample rate in Hz (default 1000)
            format: Streaming format - 0=ProtoBuf, 1=JSON (default), 2=CSV
            per_channel_dispatch: Optional callback invoked once per
                channel value in every produced :class:`Sample`. Signature:
                ``(channel_index, value, epoch_seconds) -> None`` where
                ``epoch_seconds`` is wall-clock time anchored against the
                first sample's relative timestamp (see
                :meth:`_dispatch_per_channel`). When set (typically by
                :meth:`NyquistDevice.create_stream`), the streaming thread
                fans the sample out to per-channel ``SampleReceived``
                handlers without callers having to wire dispatch
                themselves. Closes daqifi-python-core#107.
            buffer_size: Most samples :attr:`buffer` keeps (the newest);
                older ones are dropped so a long-running stream can't
                exhaust memory. Default :data:`DEFAULT_BUFFER_SIZE`; None
                keeps all.
            integer_units: What integer JSON/CSV values mean. The firmware
                writes volts with decimals by default (VoltagePrecision >=
                1), integer millivolts at precision 0, and integer raw ADC
                codes in raw mode, and the stream doesn't say which integer
                form it is. "millivolts" or "raw" says; None reads integers
                as raw codes and warns once per session. Decimal values are
                volts either way.
        """
        if buffer_size is not None and buffer_size <= 0:
            raise ValueError("buffer_size must be positive (or None for unbounded).")
        from .sdcard import _INTEGER_UNITS

        if integer_units not in _INTEGER_UNITS:
            raise ValueError(
                "integer_units must be None, 'raw' or 'millivolts' "
                f"(got {integer_units!r})"
            )
        self._integer_units = integer_units
        self._value_reader = self._new_value_reader()
        self._comm = comm
        self._channels = channels
        self._sample_rate = sample_rate
        self._format = format
        self._state = StreamState.STOPPED
        self._buffer = SampleBuffer(
            channels=channels, sample_rate=sample_rate, max_samples=buffer_size
        )
        self._callback: Optional[Callable[[Sample], None]] = None
        self._per_channel_dispatch = per_channel_dispatch
        # Anchor for relative-device-time → wall-clock conversion in
        # _dispatch_per_channel. Lazily set on the first dispatched
        # sample. Sample.timestamp is device-relative seconds in the
        # common path (ts/tick_hz) and only falls back to time.time()
        # when the firmware tick rate hasn't arrived yet — anchoring at
        # first dispatch handles both. _dispatch_last_sample_ts tracks
        # the previous sample's relative timestamp so the re-anchor
        # guard fires on per-sample anomalies (a single backwards step
        # or a >60s jump from one sample to the next), not on
        # cumulative elapsed time — Qodo PR #142 pass 5 #4 caught the
        # cumulative-delta version which would re-anchor every 60s on a
        # healthy stream.
        self._dispatch_sample_base: Optional[float] = None
        self._dispatch_epoch_base: Optional[float] = None
        self._dispatch_last_sample_ts: Optional[float] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Whether the device has been sent START and not yet STOP: stop()
        # must still send STOP after a callback error moved the state to
        # ERROR, or the board keeps streaming.
        self._device_streaming = False
        self._read_buffer = ""  # Buffer for partial data (JSON/CSV)
        self._tick_rate_hz = None  # Tick rate from metadata header (for timestamp conversion)
        # The device's tick counter is 32 bits and rolls over (about every
        # 102 s at 42 MHz). Ticks are unwrapped per session; see
        # _unwrap_tick.
        self._last_raw_tick: Optional[int] = None
        self._unwrapped_tick = 0
        # Protobuf decode state. Lazily initialised on the first
        # _read_sample_protobuf call so callers using JSON/CSV pay
        # nothing for the import.
        #
        # _pb_buffer is a bytearray (not bytes) so per-read appends
        # are O(chunk) instead of O(total) — at 50 kHz the bytes
        # path would copy the whole buffer on every poll.
        self._pb_buffer: bytearray = bytearray()
        # An empty channel selection means "all enabled channels", kept as
        # intent across restarts rather than frozen to the first session's.
        self._all_channels = not self._channels
        # Channels enabled on the device when streaming started (ascending),
        # or None before the first start(). See _samples_from_pb_message.
        self._device_channels: Optional[List[int]] = None
        self._pb_parser: Optional[ProtobufMessageParser] = None
        self._pending_pb_samples: Deque[Sample] = deque()
        # Set by a parser when it takes a complete record off its buffer;
        # tells a record that gave no sample (metadata, digital-only,
        # malformed) from a poll that found nothing. See _poll_sample.
        self._consumed_record = False

    def _new_value_reader(self):
        """The firmware value rule (shared with the SD-log parsers); a new
        one per session, so the unspecified-integers warning repeats per
        session."""
        from .sdcard import _FirmwareValueReader

        return _FirmwareValueReader(
            None, self._integer_units, warning=_LIVE_INTEGER_UNITS_WARNING
        )

    @property
    def state(self) -> StreamState:
        """Get current streaming state"""
        return self._state

    @property
    def channels(self) -> List[int]:
        """Get list of channels being streamed"""
        return self._channels.copy()

    @property
    def sample_rate(self) -> int:
        """Get configured sample rate"""
        return self._sample_rate

    @property
    def buffer(self) -> SampleBuffer:
        """The newest samples collected this session, at most ``buffer_size``
        (see :class:`SampleBuffer`); cleared by :meth:`start`."""
        return self._buffer

    def start(self, callback: Optional[Callable[[Sample], None]] = None):
        """
        Start streaming.

        Args:
            callback: Optional callback function called for each sample
                     Signature: callback(sample: Sample) -> None

        Raises:
            CommandError: If streaming cannot be started
            RuntimeError: If already streaming

        Example:
            # Blocking mode
            stream.start()
            samples = stream.read(num_samples=100)

            # Callback mode
            def on_sample(sample):
                print(sample)
            stream.start(callback=on_sample)
        """
        if self._state == StreamState.RUNNING:
            raise RuntimeError("Streaming already started")
        if self._thread is not None and self._thread.is_alive():
            # A previous session's worker outlived stop()'s join (a callback
            # still running); a second worker would read the same port and
            # parser buffer.
            raise RuntimeError(
                "The previous streaming thread is still running (its callback "
                "has not returned); wait for it before starting again."
            )

        self._callback = callback
        # A new event per session, not a shared one cleared here: a worker
        # of an earlier session (even one whose thread bootstrap resumes only
        # now) keeps its own, already-set event and never reads again.
        self._stop_event = threading.Event()
        self._buffer.clear()
        self._value_reader = self._new_value_reader()  # warn once per session
        self._read_buffer = ""  # Clear read buffer (JSON/CSV)
        self._last_raw_tick = None  # a new session unwraps from its own start
        # Clear protobuf decode state for the same reason — leftover
        # bytes from a previous stop()'d session would otherwise be
        # decoded as the start of the new stream, and queued samples
        # from before stop() would surface ahead of any new data
        # (Qodo PR #149 /agentic_review #2). Reset _pb_parser too so
        # every PB lifecycle field clears symmetrically; the parser
        # itself is stateless (a thin wrapper around the factory
        # callable), but re-creating it on demand keeps the lifecycle
        # uniform and trivial to reason about (Qodo /improve pass 2 #2).
        self._pb_buffer.clear()
        self._pending_pb_samples.clear()
        self._pb_parser = None
        # Reset the per-channel dispatch anchor so a stop/start of the
        # same StreamManager instance starts a fresh epoch base. Without
        # this, the previous session's anchor gets reused and downstream
        # epoch_seconds drift by the gap between sessions (Qodo PR #142
        # pass 4 inline finding).
        self._dispatch_sample_base = None
        self._dispatch_epoch_base = None
        self._dispatch_last_sample_ts = None

        try:
            # Verify at least one channel is enabled (firmware bug workaround)
            # Without this check, firmware crashes with TLB exception
            enabled_mask = self._comm.send_query("ENABle:VOLTage:DC?")
            mask = int(enabled_mask)
            # The channels the device will actually send, in ascending
            # order: a protobuf frame's values follow this order, whatever
            # channel list the caller asked for.
            self._device_channels = [bit for bit in range(mask.bit_length()) if mask >> bit & 1]
            enabled_count = len(self._device_channels)
            if self._all_channels:
                # No selection means every enabled channel, re-read on each
                # start (the enabled set may change between sessions); record
                # them so .channels and the buffer's exports name what samples
                # carry.
                self._channels = list(self._device_channels)
                self._buffer.channels = list(self._device_channels)
            if enabled_count == 0:
                raise ConfigurationError(
                    "No ADC channels enabled. Enable at least one channel before streaming.\n"
                    "Example: device.analog_inputs[0].enabled = True"
                )

            # Set streaming format
            # Format: 0=ProtoBuf, 1=JSON, 2=CSV
            self._comm.send_set(scpi.Streaming.FORMAT_SET, self._format)

            # Send SCPI command to start streaming with frequency parameter
            # Don't issue any other commands during streaming (only STReam:STOP allowed)
            start_cmd = f"{scpi.Streaming.START} {self._sample_rate}"
            # Set before sending: if the send fails midway the device may
            # still have started, and a later stop() should send STOP.
            self._device_streaming = True
            response = self._send_start(start_cmd)

            # Validate the START response before transitioning to RUNNING.
            # Without this, a device-side rejection (e.g. unknown command
            # on older firmware that hasn't picked up the SYST:STR:START
            # alias, or an "execution error" from invalid state) would
            # silently leave us in RUNNING and surface later as read()
            # timeouts instead of a clear start failure. (Qodo PR #6
            # pass 1 #2.)
            #
            # NB: use the prefix-checking contains_scpi_error() on a
            # single status line — is_error_response() does a substring
            # scan over the whole buffer and false-positives on stream
            # payload that happens to include the word "ERROR" (Qodo
            # PR #6 pass 2 #1 — regression from the naïve guard above).
            first_line = response
            if scpi.contains_scpi_error([first_line]):
                raise CommandError(
                    f"Streaming START rejected by device: {first_line.strip()!r}"
                )

            self._state = StreamState.RUNNING

            # Start background thread if callback provided
            if callback is not None:
                worker = threading.Thread(
                    target=self._streaming_thread, args=(self._stop_event,), daemon=True
                )
                # Published before it runs, and kept even if start() raises:
                # its first callback may call stop() and start(), which must
                # see it, and an interrupt can land inside start() after the
                # native thread launched (before it is visibly alive), so
                # "did it start" can't be judged here. A thread that really
                # never started is not alive: start() admits a new worker
                # and stop() drops it without joining.
                self._thread = worker
                worker.start()

        except Exception as e:
            self._state = StreamState.ERROR
            # Don't double-wrap the explicit CommandError raised inside
            # the try (e.g. the START-rejection guard above). Re-raise it
            # as-is so callers see "Streaming START rejected by device:
            # ..." instead of "Failed to start streaming: Streaming
            # START rejected by device: ...". For any other exception,
            # wrap with `from e` to preserve the original traceback.
            # (Qodo PR #6 pass 3 #1.)
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to start streaming: {e}") from e

    def _send_start(self, start_cmd: str) -> str:
        """Send START and hand everything that arrives with it to the stream.

        ``send_command`` would read and discard those bytes: the CSV/JSON
        metadata header (tick rate, column-to-channel mapping) and the first
        protobuf frames. Only a leading echo of the command and the prompt
        are dropped; the rest seeds the decode buffers. Returns the first
        text line, for the START-rejection check.
        """
        port = self._comm._serial
        port.write((start_cmd + "\r").encode("ascii"))
        port.flush()
        time.sleep(0.2)
        # As send_command: wait up to its timeout for the first bytes (a
        # rejection can arrive after the 200 ms settle), then read until the
        # line goes quiet.
        raw = bytearray()
        timeout = getattr(self._comm, "timeout", 0.3)
        if not isinstance(timeout, (int, float)):
            timeout = 0.3
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waiting = port.in_waiting
            if waiting:
                raw.extend(port.read(waiting))
                time.sleep(0.05)
                if not port.in_waiting:
                    break
            else:
                time.sleep(0.01)
        # If the read window ended inside the echo or the prompt (b"DAQIFI>"
        # with its space still in flight), read on briefly: deciding on a
        # partial console prefix would leave its tail in the stream.
        echo_line = start_cmd.encode("ascii") + b"\r\n"
        extra_deadline = time.monotonic() + 0.5
        while time.monotonic() < extra_deadline and self._ends_inside_console_prefix(
            bytes(raw), echo_line
        ):
            waiting = port.in_waiting
            if waiting:
                raw.extend(port.read(waiting))
            else:
                time.sleep(0.01)
        head = self._strip_console_prefix(bytes(raw), echo_line)
        if self._format == 0:
            self._pb_buffer.extend(head)
        else:
            self._read_buffer += head.decode("utf-8", errors="ignore")
        # For the rejection check: the first line with content, past blank
        # lines and a prompt on a line of its own.
        for line in head.decode("ascii", errors="ignore").splitlines():
            line = line.strip()
            if line.upper().startswith("DAQIFI>"):
                line = line[len("DAQIFI>"):].strip()
            if line and line != start_cmd:  # an echo the strip didn't reach
                return line
        return ""

    @staticmethod
    def _strip_prompt(data: bytes) -> bytes:
        """``data`` without one leading prompt. The firmware prompt is
        exactly "DAQIFI> " (microrl _PROMPT_DEFAULT, no line ending); a
        prompt on a line of its own loses its CR LF with it, as a unit:
        never a bare CR or LF, which can be a protobuf frame's length byte."""
        for prompt in _CONSOLE_PROMPTS:
            if data.startswith(prompt):
                return data[len(prompt):]
        return data

    @staticmethod
    def _is_console_noise(data: bytes) -> bool:
        """True if ``data`` is only line endings and prompts."""
        rest = data.replace(b"DAQIFI> ", b"").replace(b"DAQIFI>", b"")
        return not rest.strip(b"\r\n")

    @classmethod
    def _find_echo(cls, data: bytes, echo_line: bytes) -> int:
        """Where the START echo starts, if only line endings and prompts
        come before it; else -1."""
        position = data.find(echo_line)
        if position >= 0 and cls._is_console_noise(data[:position]):
            return position
        return -1

    @classmethod
    def _strip_console_prefix(cls, data: bytes, echo_line: bytes) -> bytes:
        """Drop the console output that can lead the START reply.

        When the echo of START is there, everything before it may be only
        line endings and prompts (a prompt pending from the previous command,
        blank lines), and that goes with the echo, as does one prompt after
        it. The console (microrl) ends the echo with exactly CR LF (ENDL,
        _ENDL_CRLF). Without an echo, line endings go only together with a
        prompt after them, which confirms they are console output: bare CR
        or LF bytes can be a protobuf frame's length byte."""
        position = cls._find_echo(data, echo_line)
        if position >= 0:
            # After the echo, the same rule: line endings go only together
            # with the prompt after them.
            rest = data[position + len(echo_line):]
            return rest[cls._console_prefix_end(rest):]
        return data[cls._console_prefix_end(data):]

    @classmethod
    def _console_prefix_end(cls, data: bytes) -> int:
        """End of the longest leading run of line endings and prompts that
        ends with a complete prompt; 0 if there is none."""
        end = 0
        position = data.find(b"DAQIFI>")
        while 0 <= position < 256:
            for prompt in _CONSOLE_PROMPTS:  # longest forms first
                if data.startswith(prompt, position):
                    candidate = position + len(prompt)
                    if cls._is_console_noise(data[:candidate]):
                        end = candidate
                    break
            position = data.find(b"DAQIFI>", position + 1)
        return end

    @classmethod
    def _ends_inside_console_prefix(cls, data: bytes, echo_line: bytes) -> bool:
        """True if ``data`` stops part-way through the console output that
        may lead the START reply: in the echo, in a prompt, or with only
        line endings and prompts so far (the echo may follow)."""
        position = cls._find_echo(data, echo_line)
        if position >= 0:
            rest = data[position + len(echo_line):]
            # Nothing after the echo yet, only line endings so far, or a
            # prompt still arriving ("DAQIFI> ", or a prompt line split after
            # its CR). The console prints a prompt after the command, so wait
            # for it rather than let it reach the decoder.
            if not rest or (cls._is_console_noise(rest) and not cls._console_prefix_end(rest)):
                return True
            return cls._ends_in_partial_console(rest)
        if data and cls._is_console_noise(data):
            return True
        return cls._ends_in_partial_console(data, echo_line)

    @classmethod
    def _ends_in_partial_console(cls, data: bytes, echo_line: bytes = b"") -> bool:
        """True if ``data`` is line endings and prompts followed by the
        start of a prompt (or of ``echo_line``) still arriving. A partial
        can only be the last few bytes."""
        candidates = [prompt for prompt in _CONSOLE_PROMPTS] + ([echo_line] if echo_line else [])
        longest = max(len(candidate) for candidate in candidates)
        for split in range(max(0, len(data) - longest), len(data)):
            head, tail = data[:split], data[split:]
            if any(candidate.startswith(tail) for candidate in candidates) and cls._is_console_noise(head):
                return True
        return False

    def stop(self):
        """
        Stop streaming.

        Example:
            stream.start()
            time.sleep(10)
            stream.stop()
        """
        if self._state != StreamState.RUNNING and not self._device_streaming:
            return

        # Signal thread to stop
        self._stop_event.set()

        # Wait for the worker, unless this is the worker (stop() called
        # from the callback): it ends once the callback returns. A worker
        # still alive after the join keeps its reference, so start() can
        # refuse to run a second one beside it.
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=2.0)
            except RuntimeError:
                # Not started, or still bootstrapping after an interrupted
                # start(): nothing to wait for, and it won't read (its
                # session's event is set).
                pass
        if thread is not None and not thread.is_alive():
            self._thread = None

        try:
            # Send SCPI command to stop streaming
            self._comm.send_command(scpi.Streaming.STOP)
            self._device_streaming = False
            self._state = StreamState.STOPPED
        except Exception as e:
            self._state = StreamState.ERROR
            # Mirror the start() handler: don't double-wrap CommandError,
            # and preserve the traceback via `from e` for other types.
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to stop streaming: {e}") from e

    def read(self, num_samples: int = 1, timeout: Optional[float] = None) -> List[Sample]:
        """
        Read samples from stream (blocking).

        Args:
            num_samples: Number of samples to read
            timeout: Total seconds to wait for all ``num_samples``. None
                (the default) allows twice their duration at the configured
                sample rate, plus one second.

        Returns:
            List of samples

        Raises:
            RuntimeError: If not streaming
            TimeoutError: If ``num_samples`` didn't arrive within ``timeout``

        Example:
            stream.start()
            samples = stream.read(num_samples=100)
            for sample in samples:
                print(sample)
        """
        if self._state != StreamState.RUNNING:
            raise RuntimeError("Not streaming")

        if timeout is None:
            timeout = 2.0 * num_samples / max(self._sample_rate, 1) + 1.0
        samples = []
        deadline = time.monotonic() + timeout

        while len(samples) < num_samples:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Timeout reading {num_samples} samples: {len(samples)} "
                    f"arrived in {timeout:.3g} s"
                )

            # Try to read sample
            sample = self._poll_sample()
            if sample is _SKIPPED:
                continue
            if sample is not None:
                samples.append(sample)
                self._buffer.add_sample(sample)
                # Per-channel dispatch must fire from blocking reads too
                # — silently skipping it left polling-mode callers' channel
                # handlers stuck on stale samples (Qodo PR #142 pass 3 #1).
                self._dispatch_per_channel(sample)
            else:
                # Back off only when idle: a delay after every sample made
                # the reader fall behind a board at its configured rate
                # (#192).
                time.sleep(_IDLE_POLL_SECONDS)

        return samples

    def read_all(self, duration: float) -> SampleBuffer:
        """
        Read every sample that arrives during ``duration`` seconds.

        Returns after ``duration`` seconds whether or not the device sent
        anything (a stalled stream gives an empty buffer, never a hang).

        Args:
            duration: Duration in seconds

        Returns:
            SampleBuffer with the samples read in that time (only those)

        Example:
            stream.start()
            buffer = stream.read_all(duration=10.0)
            buffer.to_csv("data.csv")
        """
        if self._state != StreamState.RUNNING:
            raise RuntimeError("Not streaming")
        collected = SampleBuffer(sample_rate=self._sample_rate)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            sample = self._poll_sample()
            if sample is _SKIPPED:
                continue
            if sample is not None:
                collected.add_sample(sample)
                self._buffer.add_sample(sample)
                self._dispatch_per_channel(sample)
            else:
                time.sleep(0.001)
        # Channels as known now: a CSV stream names its channels in a header
        # that arrives with the data. Without any, those the samples carry.
        collected.channels = list(self._channels) or sorted(
            {ch for sample in collected for ch in sample.channels}
        )
        return collected

    def _poll_sample(self) -> Any:
        """One poll of the stream: a :class:`Sample`; ``_SKIPPED`` when a
        complete record was taken but gave no sample (metadata, a
        digital-only or malformed record); or None when nothing was there.

        Callers wait only on None: a skipped record means more may be
        queued right behind it.
        """
        self._consumed_record = False
        sample = self._read_sample()
        if sample is None and self._consumed_record:
            return _SKIPPED
        return sample

    def _read_sample(self) -> Optional[Sample]:
        """
        Read a single sample from device.

        Dispatches to format-specific parser based on self._format.

        Returns:
            Sample, or None if none came of this read (a parser that took a
            record without producing a sample sets ``_consumed_record``)
        """
        if self._format == 0:
            return self._read_sample_protobuf()
        elif self._format == 1:
            return self._read_sample_json()
        elif self._format == 2:
            return self._read_sample_csv()
        else:
            raise ValueError(f"Unknown streaming format: {self._format}")

    def _read_sample_json(self) -> Optional[Sample]:
        """
        Parse JSON streaming data from the device.

        JSON format from firmware (new):
        {"meta":{"dev":"Nyquist NQ1","sn":"0123...","tick_hz":1000000}}
        {"ts":12345,"ai":[{"ch":0,"val":5000},{"ch":1,"val":3300}]}

        Field names:
        - ts: timestamp (device ticks)
        - ai: analog input array
        - di: digital input array
        - val: value (millivolts for ADC)
        - meta: metadata header (first message only)

        Returns:
            Sample or None if no data available
        """
        import json

        try:
            # Read available data from serial port
            if self._comm._serial.in_waiting > 0:
                chunk = self._comm._serial.read(self._comm._serial.in_waiting).decode('utf-8', errors='ignore')
                self._read_buffer += chunk

            # Try to find complete JSON object
            start_idx = self._read_buffer.find('{')
            if start_idx == -1:
                return None

            # Find matching closing brace
            brace_count = 0
            end_idx = -1
            for i in range(start_idx, len(self._read_buffer)):
                if self._read_buffer[i] == '{':
                    brace_count += 1
                elif self._read_buffer[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_idx = i + 1
                        break

            if end_idx == -1:
                # Incomplete JSON, keep buffering
                return None

            # Extract and parse JSON
            json_str = self._read_buffer[start_idx:end_idx]
            self._read_buffer = self._read_buffer[end_idx:]  # Remove parsed data
            self._consumed_record = True

            _LOGGER.debug("JSON data received: %s", json_str)

            data = json.loads(json_str)

            # Skip metadata header (first message of session)
            if 'meta' in data:
                # Extract and store tick rate for future timestamp conversion
                if 'tick_hz' in data['meta']:
                    self._tick_rate_hz = data['meta']['tick_hz']
                    _LOGGER.debug("Metadata received: %s", data['meta'])
                return None  # Not a sample, skip

            # Parse ADC data (new field name: 'ai' not 'adc'). Values follow
            # the firmware's rule (JSON_Encoder.c): volts written with
            # decimals by default, integers only at precision 0 (millivolts)
            # or in raw mode (codes); see integer_units.
            channels_data = {}
            units = None
            if 'ai' in data:
                ch_ids = [ch_data['ch'] for ch_data in data['ai']]
                read = self._read_values([ch_data['val'] for ch_data in data['ai']], json_str)
                if read is None:
                    return None  # mixes decimals and integers: not firmware output
                values, units = read
                channels_data = dict(zip(ch_ids, values))

            if not channels_data:
                # A digital-only record ({"ts":..,"di":[..]}: the firmware
                # sends one per DIO read when DIO streaming is on) has no
                # analog value; it isn't an analog sample, and must not
                # become a fake 0 V one.
                return None

            # Get timestamp from firmware (new field: 'ts' not 'timestamp')
            if 'ts' in data and self._tick_rate_hz:
                # Convert device ticks to seconds using tick rate from metadata
                timestamp = self._unwrap_tick(data['ts']) / self._tick_rate_hz
            else:
                # Fallback to current time if no tick rate available yet
                timestamp = time.time()

            # Create sample
            sample = Sample(
                timestamp=timestamp,
                channels=channels_data,
                units=units,
            )
            return sample

        except json.JSONDecodeError:
            # The malformed object was already cut from the buffer; the
            # complete objects behind it are still good.
            _LOGGER.warning("Skipping a malformed JSON record")
            return None
        except Warning:
            raise  # a promoted warning isn't a malformed record
        except Exception as e:
            _LOGGER.warning("Error reading sample: %s", e)
            return None

    def _read_values(self, raw, record: str):
        """The value reader's result for ``raw``. Its once-per-session
        warning, promoted to an error (``simplefilter('error')``), reaches
        the caller with ``record`` put back, so the next read returns it."""
        try:
            return self._value_reader.read(raw)
        except Warning:
            self._read_buffer = record + self._read_buffer
            raise

    def _read_sample_csv(self) -> Optional[Sample]:
        """
        Parse CSV streaming data from the device.

        CSV format from firmware (new with metadata header):
        # Device: Nyquist NQ1
        # Serial Number: 0123456789ABCDEF
        # Timestamp Tick Rate: 1000000 Hz
        ain0_ts,ain0_val,ain4_ts,ain4_val,ain7_ts,ain7_val,dio_ts,dio_val
        798519461,2773,798519465,2801,798519469,2795,798520000,15

        Only enabled channels are output. Values are in millivolts.

        Returns:
            Sample or None if no data available
        """
        try:
            # Read available data from serial port
            if self._comm._serial.in_waiting > 0:
                chunk = self._comm._serial.read(self._comm._serial.in_waiting).decode('utf-8', errors='ignore')
                self._read_buffer += chunk

            # Look for complete line (ends with \n)
            newline_idx = self._read_buffer.find('\n')
            if newline_idx == -1:
                return None  # Incomplete line, keep buffering

            # Extract line
            line = self._read_buffer[:newline_idx].strip()
            self._read_buffer = self._read_buffer[newline_idx + 1:]  # Remove parsed data
            self._consumed_record = True

            if not line:
                return None  # Empty line

            # Skip metadata header lines (# Device, # Serial Number, # Timestamp Tick Rate)
            if line.startswith('#'):
                # Parse tick rate from metadata header
                if "Timestamp Tick Rate:" in line:
                    try:
                        # Format: "# Timestamp Tick Rate: 1000000 Hz"
                        parts = line.split(':')
                        if len(parts) > 1:
                            tick_str = parts[1].strip().replace(' Hz', '')
                            self._tick_rate_hz = int(tick_str)
                            _LOGGER.debug("CSV metadata: tick rate %s Hz", self._tick_rate_hz)
                    except ValueError:
                        pass
                return None  # Skip header lines

            # Detect and parse column header line (ain0_ts,ain0_val,... or ch0_ts,ch0_val,...)
            if '_ts' in line and '_val' in line:
                # Extract channel numbers from column headers
                import re
                # Match both ain{N}_ts and ch{N}_ts patterns
                ch_pattern = re.findall(r'(?:ain|ch)(\d+)_ts', line)
                if ch_pattern:
                    self._channels = [int(ch) for ch in ch_pattern]
                    # Keep the buffer's channel list in step: exports
                    # (to_dict/to_csv/to_dataframe) read it.
                    self._buffer.channels = list(self._channels)
                    _LOGGER.debug("CSV column headers: enabled channels %s", self._channels)
                return None

            _LOGGER.debug("CSV data received: %s", line)

            # Parse CSV values
            values = line.split(',')
            if len(values) < 1:
                return None

            # Parse channel data (timestamp,value pairs for enabled channels
            # only). A value is kept as written: an integer (millivolts at
            # precision 0, or a raw code) or a decimal (volts, the firmware's
            # default); the firmware rule below decides (csv_encoder.c).
            ch_ids = []
            raw_values = []
            i = 0  # Start from first field
            channel_idx = 0
            first_timestamp = None

            while i + 1 < len(values) and channel_idx < len(self._channels):
                try:
                    if values[i] and values[i+1]:  # Both timestamp and value present
                        ch_ts = int(values[i])  # Channel timestamp
                        ch_val = _csv_value(values[i + 1])

                        # Save first valid timestamp for sample
                        if first_timestamp is None:
                            first_timestamp = ch_ts

                        # Map to actual channel ID from enabled list
                        ch_ids.append(self._channels[channel_idx])
                        raw_values.append(ch_val)

                    channel_idx += 1
                    i += 2  # Move to next channel pair
                except (ValueError, IndexError):
                    break

            channels_data = {}
            units = None
            if raw_values:
                read = self._read_values(raw_values, line + '\n')
                if read is None:
                    return None  # mixes decimals and integers: not firmware output
                values_v, units = read
                channels_data = dict(zip(ch_ids, values_v))

            # Use device timestamp (convert using tick rate from metadata)
            if first_timestamp is not None and self._tick_rate_hz:
                timestamp = self._unwrap_tick(first_timestamp) / self._tick_rate_hz
            elif first_timestamp is not None:
                # Fallback to 1MHz if tick rate not available
                timestamp = self._unwrap_tick(first_timestamp) / 1_000_000.0
            else:
                timestamp = time.time()  # Fallback to PC time if no valid channels

            # Create sample
            if channels_data:
                sample = Sample(
                    timestamp=timestamp,
                    channels=channels_data,
                    units=units,
                )
                return sample
            else:
                return None

        except Warning:
            raise  # a promoted warning isn't a malformed record
        except Exception as e:
            _LOGGER.warning("Error parsing CSV sample: %s", e)
            return None

    def _read_sample_protobuf(self) -> Optional[Sample]:
        """Decode one streaming sample from the protobuf byte stream.

        Reads any available bytes, feeds them to a
        :class:`ProtobufMessageParser` (length-prefixed
        ``DaqifiOutMessage`` frames), and returns one :class:`Sample`
        per call. When a single frame produces multiple samples
        (e.g. firmware batched several ticks), the surplus is queued
        in ``self._pending_pb_samples`` and returned on subsequent
        calls.

        Returns ``None`` if no complete frame is buffered yet — the
        caller (``read()`` / ``_streaming_thread``) re-invokes after
        the next read window.

        Closes #148. The previous implementation hex-dumped the
        first 32 bytes and returned a hardcoded
        ``Sample(channels={0: 0.0})`` placeholder, so every PB
        consumer received the same fake value regardless of the
        actual ADC reading.
        """
        if self._pending_pb_samples:
            return self._pending_pb_samples.popleft()

        # Wrap the entire read+parse pipeline so any transport hiccup
        # (in_waiting probe, serial.read I/O error) or parser failure
        # surfaces as a CommandError. Without this, blocking read()
        # callers would see raw pyserial / protobuf exceptions and
        # the callback thread's generic "Streaming error" message
        # would mask the actual fault site (Qodo PR #149
        # /agentic_review #3).
        try:
            in_waiting = self._comm._serial.in_waiting

            if in_waiting > 0:
                self._pb_buffer.extend(self._comm._serial.read(in_waiting))

            if not self._pb_buffer:
                return None

            if self._pb_parser is None:
                # Lazy import keeps the protobuf generated module out
                # of the CSV/JSON hot path. The factory mirrors
                # ``DaqifiStreamingDevice._default_message_factory``
                # so both paths decode identically.
                from ._internal.proto.DaqifiOutMessage_pb2 import (
                    DaqifiOutMessage,
                )
                self._pb_parser = ProtobufMessageParser(
                    message_factory=lambda payload: DaqifiOutMessage.FromString(
                        bytes(payload)
                    )
                )

            # Pass the bytearray directly — the parser uses index
            # and slice operations both of which work on bytearray
            # without conversion. Wrapping with bytes(...) here
            # would re-copy the entire buffer on every parse, exactly
            # the O(n) cost the bytearray switch was meant to avoid.
            # (Qodo PR #149 /agentic_review pass 3 #3.)
            messages, consumed = self._pb_parser.parse_messages(
                self._pb_buffer
            )
            if consumed:
                del self._pb_buffer[:consumed]
                self._consumed_record = True
            elif len(self._pb_buffer) > self._PB_BUFFER_HARD_CAP:
                # Parser made no forward progress and the buffer keeps
                # growing — the wire is desynced (firmware reboot mid-
                # stream, garbage from another device, transient line
                # corruption). Drop the oldest half and keep the tail:
                # any in-flight frame is much more likely to be near
                # the current write pointer than at the start of the
                # accumulated garbage. The parser's resync-on-
                # malformed logic (advance one byte at a time) will
                # recover from the kept tail.
                #
                # max(1, …) guards the monkeypatched-tiny-cap case
                # where _PB_BUFFER_HARD_CAP // 2 == 0 would make
                # del buf[:-0] a no-op and silently disable the
                # safety mechanism (Qodo PR #149 /agentic_review
                # pass 3 #6).
                half = max(1, self._PB_BUFFER_HARD_CAP // 2)
                del self._pb_buffer[:-half]

            for inbound in messages:
                for sample in self._samples_from_pb_message(inbound.data):
                    self._pending_pb_samples.append(sample)
        except CommandError:
            # Already in the right shape — don't double-wrap.
            raise
        except Exception as e:
            raise CommandError(f"Failed to read PB stream: {e}") from e

        if self._pending_pb_samples:
            return self._pending_pb_samples.popleft()
        return None

    def _unwrap_tick(self, raw_tick: int) -> int:
        """Place a raw 32-bit device tick on this session's unbroken scale.

        Each tick is placed by its signed 32-bit distance from the previous
        one, so a rollover continues upwards instead of jumping back to
        zero, and a slightly older tick (a sample queued earlier) steps back
        a little. The session's first tick keeps its raw value (#193).

        Limit: the counter only gives the step modulo 2^32, so a gap in the
        device's own samples longer than half a period (2^31 ticks: 42.9 s
        at 50 MHz, 51.1 s at 42 MHz) reads as a step back. A running stream
        doesn't produce one at any configured rate, and a new session starts
        afresh. Host time was tried as a second source, but pyserial gives
        no arrival time, and every way of estimating it misjudged ordinary
        pauses as whole periods.
        """
        raw = int(raw_tick) & _TICK_MASK
        if self._last_raw_tick is None:
            self._unwrapped_tick = raw
        else:
            delta = (raw - self._last_raw_tick) & _TICK_MASK
            if delta >= _TICK_HALF_RANGE:
                delta -= _TICK_RANGE
            self._unwrapped_tick += delta
        self._last_raw_tick = raw
        return self._unwrapped_tick

    def _samples_from_pb_message(self, message: Any) -> List["Sample"]:
        """Convert a parsed ``DaqifiOutMessage`` into one or more
        :class:`Sample` objects.

        - Prefers ``analog_in_data_float`` (USB firmware, pre-scaled
          to volts) over ``analog_in_data`` (WiFi firmware, raw ADC
          counts). The raw path is exposed unscaled — callers that
          need volts on WiFi should go through ``DaqifiStreamingDevice``
          which carries the calibration coefficients.
        - Maps payload positions to channel indices via
          ``self._channels`` (the channel list the caller started
          streaming with). ``self._channels[0]`` owns
          ``analog_in_data_float[0]``, etc.
        - Computes ``Sample.timestamp`` from ``msg_time_stamp /
          tick_rate_hz`` when the tick rate is known, falling back
          to ``time.time()`` otherwise. The CSV path establishes
          ``_tick_rate_hz`` from the header line; the protobuf
          stream has no equivalent header, so callers needing
          precise device-relative timestamps should set it
          out-of-band (typically via the device's status message).
        - Handles batched frames where ``len(analog_in_data_float)``
          is a multiple of ``len(self._channels)`` greater than one:
          each block of ``len(channels)`` values becomes its own
          ``Sample``, with per-sample timestamps from
          ``analog_in_data_ts`` when available.
        """
        values = list(getattr(message, "analog_in_data_float", None) or [])
        if not values:
            values = [float(v) for v in (
                getattr(message, "analog_in_data", None) or []
            )]
        # A frame carries one value per channel ENABLED ON THE DEVICE, in
        # ascending channel order. Mapping by the caller's list instead
        # relabelled channel 1's data as channel 0 and doubled the apparent
        # rate whenever the two differed (#162). The caller's list then
        # selects which of those channels the sample keeps.
        source = self._device_channels or self._channels
        if not values or not source:
            return []
        wanted = set(self._channels) if self._channels else set(source)

        num_channels = len(source)
        if len(values) % num_channels != 0:
            # Partial / misaligned frame — refuse to guess at the
            # per-channel split. The CSV/JSON paths silently drop
            # malformed lines too.
            return []

        per_sample_ts = list(getattr(message, "analog_in_data_ts", None) or [])
        msg_ts = getattr(message, "msg_time_stamp", 0)

        def _convert_ts(raw_ts: int) -> float:
            if self._tick_rate_hz and self._tick_rate_hz > 0:
                return self._unwrap_tick(raw_ts) / float(self._tick_rate_hz)
            return time.time()

        samples: List[Sample] = []
        batch_count = len(values) // num_channels
        for i in range(batch_count):
            block = values[i * num_channels : (i + 1) * num_channels]
            # analog_in_data_ts is an offset to msg_time_stamp
            # (DaqifiOutMessage.proto, field 4), not an absolute time.
            raw_ts = msg_ts + (per_sample_ts[i] if i < len(per_sample_ts) else 0)
            channels = {
                ch: float(block[idx])
                for idx, ch in enumerate(source)
                if ch in wanted
            }
            if channels:
                samples.append(Sample(timestamp=_convert_ts(raw_ts), channels=channels))
        return samples

    def _streaming_thread(self, stop_event: Optional[threading.Event] = None):
        """Background thread for callback-based streaming"""
        stop_event = stop_event if stop_event is not None else self._stop_event
        while not stop_event.is_set():
            try:
                sample = self._poll_sample()
                if sample is _SKIPPED:
                    continue
                if sample is not None:
                    self._buffer.add_sample(sample)
                    self._dispatch_per_channel(sample)
                    if self._callback is not None:
                        self._callback(sample)
                else:
                    # Wait only when there was nothing to deliver. Pacing
                    # every sample by the sample period, as before, could
                    # never keep up: reading a sample takes time too, so the
                    # backlog grew for the whole session (#192).
                    time.sleep(_IDLE_POLL_SECONDS)

            except Warning:
                # Promoted to an error, with no caller on this thread to
                # raise it to; the record was kept for the next read.
                _LOGGER.warning("Streaming warning", exc_info=True)
            except Exception:
                _LOGGER.exception("Streaming error")
                self._state = StreamState.ERROR
                break

    def _dispatch_per_channel(self, sample: Sample) -> None:
        """Fan a multi-channel :class:`Sample` out to per-channel handlers
        when ``per_channel_dispatch`` is wired (closes #107).

        ``Sample.timestamp`` is device-relative seconds in the common
        decode paths (``ts / tick_hz``), not UNIX epoch seconds — feeding
        it to ``datetime.fromtimestamp`` directly produces 1970-era
        timestamps. We anchor against the first dispatched sample's
        timestamp to recover wall-clock time:

            epoch = epoch_at_first_dispatch + (sample.ts - sample.ts_at_first_dispatch)

        This is approach B from Qodo PR #142 pass 2 — keeps
        ``Sample.timestamp`` semantics unchanged and shifts the
        epoch-anchoring concern into the dispatch closure where the
        per-channel ``DataSample`` is actually constructed.

        A misbehaving handler must not poison the streaming thread or
        starve other channels — log and continue per channel.
        """
        if self._per_channel_dispatch is None:
            return
        # NaN/inf in sample.timestamp would poison _dispatch_last_sample_ts
        # because NaN comparisons (NaN < 0, NaN > 60) all evaluate False,
        # so the re-anchor guard never triggers; subsequent epoch_seconds
        # become NaN and never recover. Drop the sample without touching
        # any state — preserves the prior good anchor for the next valid
        # sample. (Qodo PR #142 pass 6 importance 9.)
        if not math.isfinite(sample.timestamp):
            return
        if self._dispatch_sample_base is None or self._dispatch_epoch_base is None:
            self._dispatch_sample_base = sample.timestamp
            self._dispatch_epoch_base = time.time()
            self._dispatch_last_sample_ts = sample.timestamp
        # Per-sample step (NOT cumulative delta). Re-anchor on
        # backwards or >60s jumps from the previous sample — those
        # signal device tick wrap, mid-stream reset, or parser
        # reseating after a recovery window. The earlier cumulative-
        # delta guard incorrectly fired ~every 60s on a healthy stream
        # (Qodo PR #142 pass 5 #4).
        step = sample.timestamp - self._dispatch_last_sample_ts
        if step < 0.0 or step > 60.0:
            self._dispatch_sample_base = sample.timestamp
            self._dispatch_epoch_base = time.time()
        epoch_seconds = self._dispatch_epoch_base + (
            sample.timestamp - self._dispatch_sample_base
        )
        self._dispatch_last_sample_ts = sample.timestamp
        for channel_index, value in sample.channels.items():
            try:
                self._per_channel_dispatch(channel_index, value, epoch_seconds)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "per-channel dispatch raised on channel %d", channel_index
                )

    def __enter__(self):
        """Context manager entry"""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.stop()

    def __repr__(self) -> str:
        return (f"StreamManager(channels={self._channels}, "
                f"rate={self._sample_rate}Hz, state={self._state.value})")


class AsyncStreamManager:
    """
    Async stream manager for non-blocking streaming.

    Provides async/await interface for streaming operations.
    Requires Python 3.7+ with asyncio.
    """

    def __init__(
        self,
        comm: SerialInterface,
        channels: List[int],
        sample_rate: int = 1000,
        format: int = 1,
        per_channel_dispatch: Optional[Callable[[int, float, float], None]] = None,
        buffer_size: Optional[int] = DEFAULT_BUFFER_SIZE,
        integer_units: Optional[str] = None,
    ):
        """
        Initialize async stream manager.

        Args:
            comm: Serial communication interface
            channels: List of channel indices to stream
            sample_rate: Sample rate in Hz
            format: Streaming format - 0=ProtoBuf, 1=JSON (default), 2=CSV
            per_channel_dispatch: See :class:`StreamManager`. Forwarded
                to the wrapped synchronous manager.
            buffer_size: See :class:`StreamManager`.
            integer_units: See :class:`StreamManager`.
        """
        self._stream = StreamManager(
            comm,
            channels,
            sample_rate,
            format,
            per_channel_dispatch=per_channel_dispatch,
            buffer_size=buffer_size,
            integer_units=integer_units,
        )

    async def start(self):
        """Start streaming (async)"""
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, self._stream.start)

    async def stop(self):
        """Stop streaming (async)"""
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, self._stream.stop)

    async def read(self, num_samples: int = 1, timeout: Optional[float] = None) -> List[Sample]:
        """
        Read samples (async).

        Args:
            num_samples: Number of samples to read
            timeout: Total seconds for all of them; see
                :meth:`StreamManager.read`

        Returns:
            List of samples

        Example:
            async with AsyncStreamManager(comm, [0, 1], 1000) as stream:
                samples = await stream.read(100)
        """
        import asyncio
        import functools
        return await asyncio.get_event_loop().run_in_executor(
            None,
            functools.partial(self._stream.read, num_samples, timeout),
        )

    async def __aenter__(self):
        """Async context manager entry"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.stop()

    def __aiter__(self):
        """Async iterator"""
        return self

    async def __anext__(self) -> Sample:
        """
        Get next sample (async iterator).

        Example:
            async for sample in stream:
                print(sample)
        """
        import asyncio

        if self._stream.state != StreamState.RUNNING:
            raise StopAsyncIteration

        samples = await self.read(num_samples=1)
        if samples:
            return samples[0]
        raise StopAsyncIteration
