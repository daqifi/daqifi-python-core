"""
Data streaming for DAQiFi Nyquist devices.

Provides real-time data acquisition with support for multiple channels,
configurable sample rates, and data export.
"""

import math
import time
import threading
from collections import deque
from typing import List, Dict, Optional, Callable, Any, Deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ._internal.comm import SerialInterface
from ._internal import scpi
from .communication import ProtobufMessageParser
from .exceptions import CommandError, ConfigurationError


class StreamState(Enum):
    """Streaming state"""
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"



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

    def __getitem__(self, channel: int) -> float:
        """Get channel value by index"""
        return self.channels.get(channel, 0.0)

    def __repr__(self) -> str:
        ch_str = ", ".join(f"{ch}:{val:.3f}" for ch, val in sorted(self.channels.items()))
        return f"Sample(t={self.timestamp:.3f}, {ch_str})"


@dataclass
class SampleBuffer:
    """Buffer of samples"""
    samples: List[Sample] = field(default_factory=list)
    channels: List[int] = field(default_factory=list)
    sample_rate: float = 0.0
    start_time: Optional[float] = None

    def add_sample(self, sample: Sample):
        """Add sample to buffer"""
        if self.start_time is None:
            self.start_time = sample.timestamp
        self.samples.append(sample)

    def clear(self):
        """Clear buffer"""
        self.samples.clear()
        self.start_time = None

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> Sample:
        return self.samples[index]

    def __iter__(self):
        return iter(self.samples)

    def get_channel_data(self, channel: int) -> List[float]:
        """
        Get all data for a specific channel.

        Args:
            channel: Channel index

        Returns:
            List of voltage values
        """
        return [sample[channel] for sample in self.samples]

    def get_timestamps(self) -> List[float]:
        """
        Get all timestamps.

        Returns:
            List of timestamps (seconds since start)
        """
        if self.start_time is None:
            return []
        return [s.timestamp - self.start_time for s in self.samples]

    def to_dict(self) -> Dict[str, Any]:
        """
        Export to dictionary.

        Returns:
            Dictionary with timestamps and channel data
        """
        result = {
            'timestamps': self.get_timestamps(),
            'sample_rate': self.sample_rate,
            'num_samples': len(self.samples),
        }

        for ch in self.channels:
            result[f'channel_{ch}'] = self.get_channel_data(ch)

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

        with open(filename, 'w', newline='') as f:
            writer = csv.writer(f)

            # Write header
            if include_header:
                header = ['timestamp'] + [f'channel_{ch}' for ch in sorted(self.channels)]
                writer.writerow(header)

            # Write data
            timestamps = self.get_timestamps()
            for i, ts in enumerate(timestamps):
                row = [ts]
                for ch in sorted(self.channels):
                    row.append(self.samples[i][ch])
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

        with open(filename, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)

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


# Prompt forms that may lead the START reply, longest first so a prompt line
# is taken with its CR LF as a unit: the firmware's "DAQIFI> " (microrl
# _PROMPT_DEFAULT), and the same on a line of its own.
_CONSOLE_PROMPTS = (b"DAQIFI> \r\n", b"DAQIFI>\r\n", b"DAQIFI> ", b"DAQIFI>")


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
        """
        self._comm = comm
        self._channels = channels
        self._sample_rate = sample_rate
        self._format = format
        self._state = StreamState.STOPPED
        self._buffer = SampleBuffer(channels=channels, sample_rate=sample_rate)
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
        self._last_host_time = 0.0
        self._unwrapped_tick = 0
        self._host_clock: Callable[[], float] = time.monotonic
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
        """Get sample buffer (for reading collected samples)"""
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

    def read(self, num_samples: int = 1, timeout: float = 1.0) -> List[Sample]:
        """
        Read samples from stream (blocking).

        Args:
            num_samples: Number of samples to read
            timeout: Timeout in seconds per sample

        Returns:
            List of samples

        Raises:
            RuntimeError: If not streaming
            TimeoutError: If timeout exceeded

        Example:
            stream.start()
            samples = stream.read(num_samples=100)
            for sample in samples:
                print(sample)
        """
        if self._state != StreamState.RUNNING:
            raise RuntimeError("Not streaming")

        samples = []
        start_time = time.time()

        while len(samples) < num_samples:
            # Check timeout
            if time.time() - start_time > timeout * num_samples:
                raise TimeoutError(f"Timeout reading {num_samples} samples")

            # Try to read sample
            sample = self._read_sample()
            if sample is not None:
                samples.append(sample)
                self._buffer.add_sample(sample)
                # Per-channel dispatch must fire from blocking reads too
                # — silently skipping it left polling-mode callers' channel
                # handlers stuck on stale samples (Qodo PR #142 pass 3 #1).
                self._dispatch_per_channel(sample)

            time.sleep(0.001)  # Small delay to avoid busy loop

        return samples

    def read_all(self, duration: float) -> SampleBuffer:
        """
        Read all samples for specified duration.

        Args:
            duration: Duration in seconds

        Returns:
            SampleBuffer with all samples

        Example:
            stream.start()
            buffer = stream.read_all(duration=10.0)
            buffer.to_csv("data.csv")
        """
        expected_samples = int(duration * self._sample_rate)
        samples = self.read(num_samples=expected_samples)

        # Return buffer with all samples
        return self._buffer

    def _read_sample(self) -> Optional[Sample]:
        """
        Read a single sample from device.

        Dispatches to format-specific parser based on self._format.

        Returns:
            Sample or None if no data available
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

            # Show raw JSON data received (for debugging/verification)
            print(f"JSON data received: {json_str}")

            data = json.loads(json_str)

            # Skip metadata header (first message of session)
            if 'meta' in data:
                # Extract and store tick rate for future timestamp conversion
                if 'tick_hz' in data['meta']:
                    self._tick_rate_hz = data['meta']['tick_hz']
                    print(f"Metadata received: {data['meta']}")
                return None  # Not a sample, skip

            # Parse ADC data (new field name: 'ai' not 'adc')
            channels_data = {}
            if 'ai' in data:
                for ch_data in data['ai']:
                    ch_id = ch_data['ch']
                    # Firmware sends data in millivolts, convert to volts (new field: 'val' not 'data')
                    voltage = ch_data['val'] / 1000.0
                    channels_data[ch_id] = voltage

            # Get timestamp from firmware (new field: 'ts' not 'timestamp')
            if 'ts' in data and self._tick_rate_hz:
                # Convert device ticks to seconds using tick rate from metadata
                timestamp = (
                    self._unwrap_tick(data['ts'], self._tick_rate_hz)
                    / self._tick_rate_hz
                )
            else:
                # Fallback to current time if no tick rate available yet
                timestamp = time.time()

            # Create sample
            sample = Sample(
                timestamp=timestamp,
                channels=channels_data
            )
            return sample

        except json.JSONDecodeError:
            # Invalid JSON, clear buffer and try again
            self._read_buffer = ""
            return None
        except Exception as e:
            # Other error, log and return None
            print(f"Error reading sample: {e}")
            return None

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
                            print(f"CSV Metadata: Tick rate = {self._tick_rate_hz} Hz")
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
                    print(f"CSV Column Headers detected: Enabled channels = {self._channels}")
                return None

            # Show raw CSV data received (for debugging/verification)
            print(f"CSV data received: {line}")

            # Parse CSV values
            values = line.split(',')
            if len(values) < 1:
                return None

            # Parse channel data (timestamp,value pairs for enabled channels only)
            channels_data = {}
            i = 0  # Start from first field
            channel_idx = 0
            first_timestamp = None

            while i + 1 < len(values) and channel_idx < len(self._channels):
                try:
                    if values[i] and values[i+1]:  # Both timestamp and value present
                        ch_ts = int(values[i])  # Channel timestamp
                        ch_val = int(values[i + 1])  # Channel value in millivolts

                        # Save first valid timestamp for sample
                        if first_timestamp is None:
                            first_timestamp = ch_ts

                        # Convert millivolts to volts
                        voltage = ch_val / 1000.0

                        # Map to actual channel ID from enabled list
                        ch_id = self._channels[channel_idx]
                        channels_data[ch_id] = voltage

                    channel_idx += 1
                    i += 2  # Move to next channel pair
                except (ValueError, IndexError):
                    break

            # Use device timestamp (convert using tick rate from metadata)
            if first_timestamp is not None and self._tick_rate_hz:
                timestamp = (
                    self._unwrap_tick(first_timestamp, self._tick_rate_hz)
                    / self._tick_rate_hz
                )
            elif first_timestamp is not None:
                # Fallback to 1MHz if tick rate not available
                timestamp = self._unwrap_tick(first_timestamp, 1_000_000) / 1_000_000.0
            else:
                timestamp = time.time()  # Fallback to PC time if no valid channels

            # Create sample
            if channels_data:
                sample = Sample(
                    timestamp=timestamp,
                    channels=channels_data
                )
                return sample
            else:
                return None

        except Exception as e:
            print(f"Error parsing CSV sample: {e}")
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

    def _unwrap_tick(self, raw_tick: int, tick_hz: float) -> int:
        """Place a raw 32-bit device tick on this session's unbroken scale.

        The counter is only 32 bits, so the step from the previous tick is
        known only up to whole periods (2^32 ticks): its unsigned distance
        ``d`` could mean ``d + k * 2^32`` for any whole ``k``. The host's
        clock is the second source. The host time between receiving the two
        ticks, times ``tick_hz``, gives the step to expect, and the ``k``
        that comes closest to it is taken. So:

        - a rollover continues upwards;
        - a gap longer than half a period, or several, is counted;
        - a slightly older tick (a sample queued earlier) steps back a
          little.

        Samples read in a burst have almost no host time between them, and
        then this is the nearest-step rule: within half a period either way.

        The session's first tick keeps its raw value (#193).
        """
        raw = int(raw_tick) & _TICK_MASK
        now = self._host_clock()
        if self._last_raw_tick is None:
            self._unwrapped_tick = raw
        else:
            distance = (raw - self._last_raw_tick) & _TICK_MASK
            expected = max(0.0, now - self._last_host_time) * tick_hz
            k = round((expected - distance) / _TICK_RANGE)
            self._unwrapped_tick += distance + k * _TICK_RANGE
        self._last_raw_tick = raw
        self._last_host_time = now
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
                return (
                    self._unwrap_tick(raw_ts, self._tick_rate_hz)
                    / float(self._tick_rate_hz)
                )
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
                sample = self._read_sample()
                if sample is not None:
                    self._buffer.add_sample(sample)
                    self._dispatch_per_channel(sample)
                    if self._callback is not None:
                        self._callback(sample)

                time.sleep(1.0 / self._sample_rate)

            except Exception:
                import logging
                logging.getLogger(__name__).exception("Streaming error")
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
        """
        self._stream = StreamManager(
            comm,
            channels,
            sample_rate,
            format,
            per_channel_dispatch=per_channel_dispatch,
        )

    async def start(self):
        """Start streaming (async)"""
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, self._stream.start)

    async def stop(self):
        """Stop streaming (async)"""
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, self._stream.stop)

    async def read(self, num_samples: int = 1) -> List[Sample]:
        """
        Read samples (async).

        Args:
            num_samples: Number of samples to read

        Returns:
            List of samples

        Example:
            async with AsyncStreamManager(comm, [0, 1], 1000) as stream:
                samples = await stream.read(100)
        """
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None,
            self._stream.read,
            num_samples
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
