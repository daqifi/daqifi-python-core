"""
Data streaming for DAQiFi Nyquist devices.

Provides real-time data acquisition with support for multiple channels,
configurable sample rates, and data export.
"""

import time
import threading
from typing import List, Dict, Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ._internal.comm import SerialInterface
from ._internal import scpi
from .exceptions import CommandError, ConfigurationError


class StreamState(Enum):
    """Streaming state"""
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


@dataclass
class Sample:
    """Single sample across all enabled channels"""
    timestamp: float  # Seconds since epoch
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


class StreamManager:
    """
    Manages data streaming from Nyquist device.

    Provides blocking and callback-based streaming with configurable
    sample rates and channel selection.
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
        # first dispatch handles both.
        self._dispatch_sample_base: Optional[float] = None
        self._dispatch_epoch_base: Optional[float] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._read_buffer = ""  # Buffer for partial data (JSON/CSV)
        self._tick_rate_hz = None  # Tick rate from metadata header (for timestamp conversion)

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

        self._callback = callback
        self._stop_event.clear()
        self._buffer.clear()
        self._read_buffer = ""  # Clear read buffer

        try:
            # Verify at least one channel is enabled (firmware bug workaround)
            # Without this check, firmware crashes with TLB exception
            enabled_mask = self._comm.send_query("ENABle:VOLTage:DC?")
            enabled_count = bin(int(enabled_mask)).count('1')
            if enabled_count == 0:
                raise ConfigurationError(
                    "No ADC channels enabled. Enable at least one channel before streaming.\n"
                    "Example: device.analog_inputs[0].enabled = True"
                )

            # Set streaming format
            # Format: 0=ProtoBuf, 1=JSON, 2=CSV
            self._comm.send_set(scpi.Streaming.FORMAT_SET, self._format)

            # Send SCPI command to start streaming with frequency parameter
            # Don't issue any other commands during streaming (only StopStreamData allowed)
            start_cmd = f"{scpi.Streaming.START} {self._sample_rate}"
            response = self._comm.send_command(start_cmd, delay=0.2)

            self._state = StreamState.RUNNING

            # Start background thread if callback provided
            if callback is not None:
                self._thread = threading.Thread(target=self._streaming_thread, daemon=True)
                self._thread.start()

        except Exception as e:
            self._state = StreamState.ERROR
            raise CommandError(f"Failed to start streaming: {e}")

    def stop(self):
        """
        Stop streaming.

        Example:
            stream.start()
            time.sleep(10)
            stream.stop()
        """
        if self._state != StreamState.RUNNING:
            return

        # Signal thread to stop
        self._stop_event.set()

        # Wait for thread to finish
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        try:
            # Send SCPI command to stop streaming
            self._comm.send_command(scpi.Streaming.STOP)
            self._state = StreamState.STOPPED
        except Exception as e:
            self._state = StreamState.ERROR
            raise CommandError(f"Failed to stop streaming: {e}")

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
                timestamp = data['ts'] / self._tick_rate_hz
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
                timestamp = first_timestamp / self._tick_rate_hz
            elif first_timestamp is not None:
                # Fallback to 1MHz if tick rate not available
                timestamp = first_timestamp / 1_000_000.0
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
        """
        Parse ProtoBuf streaming data from the device.

        ProtoBuf format uses DaqifiOutMessage binary encoding.
        Currently in testing mode - verifies data flow without full decoding.

        Returns:
            Sample or None if no data available
        """
        try:
            # Read available binary data from serial port
            if self._comm._serial.in_waiting > 0:
                chunk = self._comm._serial.read(self._comm._serial.in_waiting)

                # For testing: display hex dump of received data
                if len(chunk) > 0:
                    hex_str = ' '.join(f'{b:02x}' for b in chunk[:32])  # First 32 bytes
                    if len(chunk) > 32:
                        hex_str += f' ... ({len(chunk)} bytes total)'
                    print(f"ProtoBuf data received: {hex_str}")

                    # Create a placeholder sample to indicate data was received
                    # In real implementation, we would decode the protobuf message here
                    sample = Sample(
                        timestamp=time.time(),
                        channels={0: 0.0}  # Placeholder - would parse from protobuf
                    )
                    return sample

            return None

        except Exception as e:
            print(f"Error reading ProtoBuf sample: {e}")
            return None

    def _streaming_thread(self):
        """Background thread for callback-based streaming"""
        while not self._stop_event.is_set():
            try:
                sample = self._read_sample()
                if sample is not None:
                    self._buffer.add_sample(sample)
                    self._dispatch_per_channel(sample)
                    if self._callback is not None:
                        self._callback(sample)

                time.sleep(1.0 / self._sample_rate)

            except Exception as e:
                print(f"Streaming error: {e}")
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
        if self._dispatch_sample_base is None:
            self._dispatch_sample_base = sample.timestamp
            self._dispatch_epoch_base = time.time()
        epoch_seconds = self._dispatch_epoch_base + (
            sample.timestamp - self._dispatch_sample_base
        )
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
