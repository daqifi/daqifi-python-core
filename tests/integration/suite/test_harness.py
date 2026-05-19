"""
Shared test harness: reliable SCPI communication + clean device state.

Every test script should use this module for consistent behavior:

    from test_harness import ReliableSCPI, StreamingMeasurement, device_reset

    device = NyquistDevice.connect(port)
    scpi = ReliableSCPI(device)
    device_reset(scpi)

STREAMING RATE MEASUREMENT:
    ALL streaming throughput calculations MUST use StreamingMeasurement.
    Never use wall-clock time around SCPI start/stop — command latency
    varies by ~1s and causes 10% measurement error.

    m = StreamingMeasurement(scpi, scpi._serial)
    m.start(freq=5000, duration=10, is_csv=True)
    print(f"SPS: {m.pc_sps}, KB/s: {m.pc_kbps}, Leaked: {m.leaked}")

    The measurement window is PC-controlled: first data arrival starts
    the clock, PC times for exactly `duration` seconds, then stop is
    sent OUTSIDE the window. PC counts CSV rows independently.

SERIAL DATA DRAINING:
    For streaming tests, always use FastReader or StreamingMeasurement
    to drain serial data at >=1ms intervals. Slow host-side reading
    causes firmware-side USB drops.
"""

import time
import sys
import threading
from pathlib import Path



class ReliableSCPI:
    """Reliable SCPI command/response over serial with binary data handling."""

    def __init__(self, device):
        self._device = device
        self._serial = device._comm._serial

    def drain(self, timeout=1.0):
        """Drain all pending serial data until quiet for timeout seconds."""
        quiet_start = time.monotonic()
        total = 0
        while time.monotonic() - quiet_start < timeout:
            n = self._serial.in_waiting
            if n > 0:
                self._serial.read(n)
                total += n
                quiet_start = time.monotonic()
            else:
                time.sleep(0.05)
        return total

    def command(self, cmd, delay=0.3):
        """Send SCPI command (no response expected)."""
        self._serial.reset_input_buffer()
        self._serial.write(f"{cmd}\r".encode('ascii'))
        self._serial.flush()
        time.sleep(delay)

    def query(self, cmd, delay=1.0):
        """Send SCPI query, return raw text response."""
        self.drain(0.3)
        self._serial.reset_input_buffer()
        self._serial.write(f"{cmd}\r".encode('ascii'))
        self._serial.flush()
        time.sleep(delay)
        chunks = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            n = self._serial.in_waiting
            if n > 0:
                chunks.append(self._serial.read(n))
                time.sleep(0.05)
            else:
                if chunks:
                    break
                time.sleep(0.1)
        raw = b''.join(chunks)
        return raw.decode('ascii', errors='ignore')

    def query_kv(self, cmd, delay=1.0):
        """Send query, return dict of key=value pairs."""
        text = self.query(cmd, delay)
        d = {}
        for line in text.split('\n'):
            if '=' in line:
                parts = line.split('=', 1)
                if len(parts) == 2:
                    try:
                        d[parts[0].strip()] = int(parts[1].strip())
                    except ValueError:
                        d[parts[0].strip()] = parts[1].strip()
        return d


class FastReader:
    """Background thread that continuously drains serial data.

    CRITICAL: USB CDC streaming requires the host to read at >=1ms intervals.
    Slow reading (e.g., 500ms polling) causes the device's USB circular buffer
    to fill, triggering packet drops on the firmware side. This was proven by
    A/B testing: same firmware, same rate -- slow reader = 1MB drops, fast
    reader = 0 drops.

    Usage:
        reader = FastReader(serial)
        reader.start()
        # ... streaming happens ...
        reader.stop()
        print(f"Received {reader.bytes_received} bytes")
    """
    def __init__(self, serial):
        self._serial = serial
        self._running = False
        self._thread = None
        self.bytes_received = 0

    def start(self):
        self._running = True
        self.bytes_received = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            n = self._serial.in_waiting
            if n > 0:
                self._serial.read(n)
                self.bytes_received += n
            else:
                time.sleep(0.001)  # 1ms polling — proven zero-drop threshold

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)


class StreamingMeasurement:
    """PC-side independent streaming rate measurement.

    Measures actual throughput using a precise PC-controlled time window
    that excludes all SCPI command latency:

    1. Start streaming (SCPI command)
    2. Wait for first data byte to arrive
    3. Open measurement window (PC clock)
    4. Count bytes and CSV samples during the window
    5. Close window after exactly `duration` seconds (PC clock)
    6. Stop streaming (SCPI command, OUTSIDE the window)

    The result is purely PC-side: no firmware counters are trusted
    for the rate calculation. Firmware stats are queried afterward
    for cross-checking and drop detection.

    Usage:
        m = StreamingMeasurement(scpi, serial)
        m.start(freq=5000, duration=10)  # blocks for duration
        print(f"PC SPS: {m.pc_sps}")
        print(f"PC KB/s: {m.pc_kbps}")
        print(f"Leaked: {m.leaked}")
        print(f"FW stats: {m.fw_stats}")
    """

    def __init__(self, scpi, serial):
        self._scpi = scpi
        self._serial = serial

        # Results (populated after start() returns)
        self.pc_bytes_total = 0       # Total bytes received (entire session)
        self.pc_bytes_window = 0      # Bytes received during measurement window
        self.pc_samples_window = 0    # CSV rows counted during measurement window
        self.window_duration = 0.0    # Actual window duration (PC clock)
        self.pc_sps = 0.0             # PC-measured sample rate
        self.pc_kbps = 0.0            # PC-measured throughput (KB/s)
        self.fw_stats = {}            # Firmware stats snapshot
        self.leaked = False           # Any data loss detected

    @staticmethod
    def _count_pb_frames(buf):
        """Count complete protobuf length-delimited frames in buf.

        PB streaming format: [varint length][payload bytes][varint length]...
        Each frame = one sample message. Returns (frame_count, leftover_bytes).
        """
        frames = 0
        i = 0
        n = len(buf)
        while i < n:
            length = 0
            shift = 0
            varint_start = i
            varint_bytes = 0
            while i < n:
                b = buf[i]
                i += 1
                varint_bytes += 1
                if varint_bytes > 10:
                    # Corrupt varint (protobuf max is 10 bytes)
                    return frames, buf[varint_start:]
                length |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            else:
                # Ran out of bytes mid-varint — return leftover from varint_start
                return frames, buf[varint_start:]

            if length == 0:
                # Zero-length frame (shouldn't happen but don't get stuck)
                continue

            # Check if full payload is available
            if i + length > n:
                # Incomplete frame — return leftover from varint_start
                return frames, buf[varint_start:]

            # Complete frame
            i += length
            frames += 1

        return frames, b''

    def start(self, freq, duration, is_csv=False, wait_for_serial=True):
        """Run the measurement. Blocks for `duration` seconds + overhead.

        Args:
            freq:     Commanded streaming frequency (Hz)
            duration: Measurement window duration (seconds, PC clock)
            is_csv:   True if CSV format, False for PB. Both formats get
                      fully independent PC-side sample counting.
            wait_for_serial: If True (default), wait for first serial byte
                      before opening window. Set False for WiFi/SD-only
                      streaming where data doesn't arrive on serial.
        """
        stop = False
        measuring = False
        first_data_time = None
        csv_header_seen = False
        leftover = b''
        count_pb = StreamingMeasurement._count_pb_frames

        # Mutable counters accessed by reader thread
        counters = {
            'bytes_total': 0,
            'bytes_window': 0,
            'samples_window': 0,
        }

        def reader():
            nonlocal first_data_time, measuring, csv_header_seen, leftover
            while not stop:
                try:
                    n = self._serial.in_waiting
                    if n > 0:
                        data = self._serial.read(n)
                        counters['bytes_total'] += len(data)
                        now = time.monotonic()

                        if first_data_time is None:
                            first_data_time = now

                        if measuring:
                            counters['bytes_window'] += len(data)
                            if is_csv:
                                # Count complete CSV rows (newline-delimited)
                                chunk = leftover + data
                                lines = chunk.split(b'\n')
                                leftover = lines[-1]
                                for line in lines[:-1]:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    if not csv_header_seen:
                                        csv_header_seen = True
                                        continue
                                    counters['samples_window'] += 1
                            else:
                                # Count PB length-delimited frames
                                chunk = leftover + data
                                frames, leftover = count_pb(chunk)
                                counters['samples_window'] += frames
                    else:
                        time.sleep(0.001)
                except Exception:
                    time.sleep(0.01)

        # Start reader + streaming
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        self._scpi.command(f'SYST:STReam:START {freq}', 0)

        # Wait for first serial data (excludes SCPI start latency).
        # For WiFi/SD-only streaming, skip this — data arrives on TCP/SD,
        # not serial. The window opens immediately after a short settle.
        if wait_for_serial:
            deadline = time.monotonic() + 10
            while first_data_time is None and time.monotonic() < deadline:
                time.sleep(0.001)

            if first_data_time is None:
                stop = True; t.join(timeout=3)
                self._scpi.command('SYST:STReam:STOP', 0)
                time.sleep(1); self._scpi.drain(2)
                self.leaked = True
                return
        else:
            time.sleep(0.5)  # Brief settle for non-serial paths

        # === MEASUREMENT WINDOW (PC-controlled, no firmware interaction) ===
        measuring = True
        window_start = time.monotonic()
        time.sleep(duration)
        window_end = time.monotonic()
        measuring = False
        self.window_duration = window_end - window_start
        # === END MEASUREMENT WINDOW ===

        # Stop streaming (outside window — doesn't affect measurement)
        self._scpi.command('SYST:STReam:STOP', 0)
        time.sleep(1)
        stop = True; t.join(timeout=3)
        self._scpi.drain(2)

        # Capture results
        self.pc_bytes_total = counters['bytes_total']
        self.pc_bytes_window = counters['bytes_window']
        self.pc_samples_window = counters['samples_window']

        if self.window_duration > 0:
            self.pc_kbps = self.pc_bytes_window / self.window_duration / 1024
            if self.pc_samples_window > 0:
                self.pc_sps = self.pc_samples_window / self.window_duration

        # Query firmware stats for cross-check and drop detection
        try:
            self.fw_stats = self._scpi.query_kv('SYST:STR:STATS?')
        except Exception:
            self.fw_stats = {}

        qd = self.fw_stats.get('QueueDroppedSamples', 0)
        ud = self.fw_stats.get('UsbDroppedBytes', 0)
        sd_d = self.fw_stats.get('SdDroppedBytes', 0)
        wd = self.fw_stats.get('WifiDroppedBytes', 0)
        self.leaked = qd > 0 or ud > 0 or sd_d > 0 or wd > 0


def _assert_expected_device(scpi):
    """Gate destructive harness ops behind a serial-number identity
    check, per the daqifi-python-core CLAUDE.md "Identity-gating
    destructive operations" rule.

    Refuses to mutate device state when:
      - ``DAQIFI_EXPECTED_SERIAL_NUMBER`` is set and does NOT match
        the connected device's ``metadata.serial_number``, OR
      - The env var is unset AND ``DAQIFI_ALLOW_UNGATED_RESET`` is
        not set to ``"1"`` (the explicit opt-out for solo rigs that
        accept the risk).

    Multiple agent sessions frequently share a workstation with
    multiple DAQiFi units attached. Mass-resetting the wrong unit
    can wipe another agent's in-flight test state.
    """
    import os
    expected = os.environ.get("DAQIFI_EXPECTED_SERIAL_NUMBER")
    if expected:
        try:
            actual = scpi._device.metadata.serial_number
        except AttributeError as e:
            raise RuntimeError(
                f"Refusing destructive harness op: couldn't read "
                f"metadata.serial_number to verify identity ({e})."
            )
        if str(actual) != str(expected):
            raise RuntimeError(
                f"Refusing destructive harness op: connected device SN "
                f"{actual!r} != expected {expected!r}. Set "
                f"DAQIFI_EXPECTED_SERIAL_NUMBER to the right unit or "
                f"detach the other(s)."
            )
        return
    if os.environ.get("DAQIFI_ALLOW_UNGATED_RESET") != "1":
        raise RuntimeError(
            "Refusing destructive harness op: neither "
            "DAQIFI_EXPECTED_SERIAL_NUMBER (the gate) nor "
            "DAQIFI_ALLOW_UNGATED_RESET=1 (the opt-out) is set. See "
            "CLAUDE.md 'Identity-gating destructive operations'."
        )


def device_reset(scpi):
    """Reset device to a clean, known state between tests.

    Call this before EVERY test to prevent state contamination.
    After this function returns, the device is:
    - Not streaming
    - Benchmark mode off
    - Test patterns off
    - SD disabled
    - All channels disabled
    - Interface set to USB
    - MemoryConfig zeroed (auto mode for next STReam:START)
    - Serial buffer drained
    """
    # Verify identity before issuing any state-changing SCPI command —
    # see CLAUDE.md "Identity-gating destructive operations".
    _assert_expected_device(scpi)
    # Stop any active streaming
    scpi.command("SYST:STReam:STOP", delay=1.0)
    scpi.drain(1.0)

    # Disable benchmark mode and test patterns
    scpi.command("SYST:STR:BENCHmark 0", delay=0.2)
    scpi.command("SYST:STR:TESTpattern 0", delay=0.2)

    # Disable SD
    scpi.command("SYST:STOR:SD:ENAble 0", delay=1.0)

    # Disable all channels
    scpi.command("ENABle:VOLTage:DC 0", delay=0.2)

    # Reset interface to USB
    scpi.command("SYST:STR:INT 0", delay=0.2)

    # Reset MemoryConfig to auto mode (all zeros)
    scpi.command("SYST:MEM:RESet", delay=0.3)

    # Final drain
    scpi.drain(0.5)
