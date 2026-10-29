"""
Main device controller for DAQiFi Nyquist devices.

Provides high-level API for device interaction.
"""

from typing import Dict, Any, Optional, Union, List
from ._internal.comm import DataSample, SerialInterface, PowerState, DeviceMetadata
from ._internal import scpi, scpi_commands
from ._internal.scpi import check_setter_response
from .channels import (
    AnalogOutputChannel,
    AnalogInputChannel,
    DigitalIOChannel,
    AnalogOutputs,
    AnalogInputs,
    DigitalIO,
)
from .streaming import StreamManager, AsyncStreamManager
from .discovery import DeviceInfo, BoardVariant
from .exceptions import (
    ConnectionError as DAQiFiConnectionError,
    CommandError,
    FeatureNotSupportedError
)


_METADATA_FIELD_NAMES = (
    "device_pn",
    "device_sn",
    "device_fw_rev",
    "device_hw_rev",
    "host_name",
    "ssid",
)


def _frame_has_metadata_fields(data: Any) -> bool:
    """Heuristic: does ``data`` (a parsed DaqifiOutMessage) look like
    a SYSInfoPB? reply rather than a streaming-status frame?

    Returns True iff at least one of the canonical SystStatusPB-only
    fields is populated. The string ``"0"`` is rejected explicitly —
    firmware 3.4.6b1 uses it as a placeholder for the *IDN? SN field
    and we don't want a future cross-wire bug to make that count as
    real metadata. For numeric / bytes fields, falsy values (``0`` /
    ``b""``) are skipped via the ``elif value:`` arm.
    """
    for name in _METADATA_FIELD_NAMES:
        value = getattr(data, name, None)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped and stripped != "0":
                return True
        elif value:
            return True
    return False


class NyquistDevice:
    """
    Main controller for DAQiFi Nyquist devices.

    Provides property-based access to device features including analog inputs,
    analog outputs (NQ3 only), digital I/O, and power management.

    Example:
        # Connect to device
        device = NyquistDevice.connect("/dev/ttyACM0")

        # Power up
        device.power_up()

        # Control DAC (NQ3 only)
        device.analog_outputs[0].voltage = 5.0

        # Configure ADC
        device.analog_inputs[0].range = "10V"
        device.analog_inputs[0].enabled = True

        # Control DIO
        device.digital_io[0].direction = "output"
        device.digital_io[0].state = True

        # Disconnect
        device.disconnect()
    """

    def __init__(self, port: str, baudrate: int = 115200):
        """
        Initialize device controller (use connect() instead).

        Args:
            port: Serial port path
            baudrate: Serial baud rate (default 115200)
        """
        self._comm = SerialInterface(port, baudrate)
        self._variant = BoardVariant.UNKNOWN
        self._device_info: Dict[str, Any] = {}
        self._connected = False
        # Protobuf-populated metadata (real SN, MAC, IP, SSID, ...).
        # Best-effort populated during `_connect()`; callers can re-
        # fetch on demand via :meth:`refresh_metadata` (e.g. after a
        # USB drop / reconnect).
        self._metadata: DeviceMetadata = DeviceMetadata()
        # Captured exception (if any) from the most recent best-
        # effort metadata refresh inside `_connect()`. Lets callers
        # observe an otherwise-silent SYSInfoPB? failure without
        # changing the non-fatal behavior.
        self._last_metadata_refresh_error: Optional[Exception] = None

        # Channel collections (created on connection)
        self._analog_outputs: Optional[AnalogOutputs] = None
        self._analog_inputs: Optional[AnalogInputs] = None
        self._digital_io: Optional[DigitalIO] = None

    @classmethod
    def connect(cls, port_or_info: Union[str, DeviceInfo], baudrate: int = 115200) -> 'NyquistDevice':
        """
        Connect to a Nyquist device.

        Args:
            port_or_info: Serial port path (str) or DeviceInfo from discovery
            baudrate: Serial baud rate (default 115200)

        Returns:
            Connected NyquistDevice instance

        Raises:
            ConnectionError: If connection fails

        Example:
            # Connect by port
            device = NyquistDevice.connect("/dev/ttyACM0")

            # Connect using discovery
            info = DeviceFinder.discover_first()
            device = NyquistDevice.connect(info)
        """
        # Extract port from DeviceInfo if provided
        if isinstance(port_or_info, DeviceInfo):
            port = port_or_info.port
        else:
            port = port_or_info

        # Create and connect device
        device = cls(port, baudrate)
        device._connect()
        return device

    def _connect(self):
        """Internal connection and initialization"""
        # Open serial connection
        if not self._comm.open():
            raise DAQiFiConnectionError(f"Failed to open serial port {self._comm.port}")

        self._connected = True

        try:
            # Query device identification
            idn = self._comm.send_query(scpi_commands.get_idn().data)
            self._device_info["idn"] = idn

            # Parse variant from IDN
            idn_upper = idn.upper()
            if "NQ3" in idn_upper:
                self._variant = BoardVariant.NQ3
            elif "NQ2" in idn_upper:
                self._variant = BoardVariant.NQ2
            elif "NQ1" in idn_upper:
                self._variant = BoardVariant.NQ1

            # Parse device info
            parts = idn.split(",")
            if len(parts) >= 1:
                self._device_info["manufacturer"] = parts[0].strip()
            if len(parts) >= 2:
                self._device_info["model"] = parts[1].strip()
            if len(parts) >= 3:
                self._device_info["serial_number"] = parts[2].strip()
            if len(parts) >= 4:
                self._device_info["firmware_version"] = parts[3].strip()

            # Initialize channel collections based on variant
            self._initialize_channels()

            # Best-effort populate `metadata` from SYSInfoPB? so callers
            # can read `device.metadata.serial_number` without an extra
            # round-trip. Failure is non-fatal — older firmware or a
            # noisy USB enumeration may not respond in 2s, in which
            # case `metadata` stays at its empty default and callers
            # can call `refresh_metadata()` explicitly.
            #
            # Must include DAQiFiConnectionError because
            # refresh_metadata translates query_binary's RuntimeError
            # into DAQiFiConnectionError to honor its docstring
            # contract — without this entry a transient I/O hiccup
            # during SYSInfoPB? would abort an otherwise-successful
            # connect via the outer except below.
            try:
                self.refresh_metadata(timeout=2.0)
                self._last_metadata_refresh_error = None
            except (CommandError, DAQiFiConnectionError, RuntimeError) as exc:
                # Observability hook — see `last_metadata_refresh_error`.
                # Behavior remains best-effort: connect still succeeds.
                self._last_metadata_refresh_error = exc

        except Exception as e:
            self._connected = False
            self._comm.close()
            raise DAQiFiConnectionError(f"Failed to initialize device: {e}")

    def _initialize_channels(self):
        """Initialize channel collections based on device variant"""
        # Analog inputs (ADC)
        if self._variant == BoardVariant.NQ1:
            num_adc = 16  # MC12bADC
        elif self._variant == BoardVariant.NQ3:
            num_adc = 8   # AD7609
        elif self._variant == BoardVariant.NQ2:
            num_adc = 16  # Assumed
        else:
            num_adc = 0

        if num_adc > 0:
            # Range is module-level on NQ3 (AD7609 register), so all
            # channels constructed here share one cache holder so a
            # `range = ...` write on any single channel invalidates
            # the bounds for all of its siblings (#99 group A).
            shared_range_cache: List[Optional[tuple]] = [None]
            adc_channels = [
                AnalogInputChannel(
                    i, self._comm, self._variant.value, range_cache=shared_range_cache
                )
                for i in range(num_adc)
            ]
            self._analog_inputs = AnalogInputs(adc_channels)

        # Analog outputs (DAC) - NQ3 only
        if self._variant == BoardVariant.NQ3:
            dac_channels = [AnalogOutputChannel(i, self._comm) for i in range(8)]
            self._analog_outputs = AnalogOutputs(dac_channels)

        # Digital I/O (all variants have 16 DIO channels)
        dio_channels = [DigitalIOChannel(i, self._comm) for i in range(16)]
        self._digital_io = DigitalIO(dio_channels)

    def disconnect(self):
        """Disconnect from device"""
        if self._connected:
            self._comm.close()
            self._connected = False

    @property
    def is_connected(self) -> bool:
        """Check if device is connected"""
        return self._connected and self._comm.is_open()

    @property
    def variant(self) -> BoardVariant:
        """
        Get device variant.

        Returns:
            BoardVariant enum (NQ1, NQ2, NQ3, or UNKNOWN)
        """
        return self._variant

    @property
    def manufacturer(self) -> str:
        """Get device manufacturer"""
        return self._device_info.get("manufacturer", "Unknown")

    @property
    def model(self) -> str:
        """Get device model"""
        return self._device_info.get("model", "Unknown")

    @property
    def serial_number(self) -> str:
        """Get device serial number from the ``*IDN?`` response.

        Note:
            Firmware 3.4.6b1 (the current release) reports the SN
            field of ``*IDN?`` as a literal ``"0"`` placeholder for
            every unit. Use :attr:`metadata.serial_number` instead
            for identity verification — that field is populated from
            the protobuf ``SYSInfoPB?`` reply and contains the real
            decimal SN.
        """
        return self._device_info.get("serial_number", "Unknown")

    @property
    def last_metadata_refresh_error(self) -> Optional[Exception]:
        """The exception captured by the most recent best-effort
        metadata refresh inside :meth:`connect`, or ``None`` if the
        refresh succeeded (or was never attempted).

        Useful for observability: after :meth:`connect` returns, a
        non-``None`` value here means SYSInfoPB? failed during
        connect-time auto-refresh and ``self.metadata`` is at its
        empty default. Callers can decide whether to retry via
        :meth:`refresh_metadata` or proceed with limited identity.
        """
        return self._last_metadata_refresh_error

    @property
    def metadata(self) -> DeviceMetadata:
        """Protobuf-populated device metadata (real SN, MAC, IP, ...).

        Returns the cached :class:`DeviceMetadata`. Populated
        automatically during :meth:`connect` via a best-effort
        ``SYSTem:SYSInfoPB?`` query; if that round-trip failed at
        connect-time the cache is left at its empty default and
        callers can re-populate it explicitly via
        :meth:`refresh_metadata`.

        Distinct from :attr:`serial_number` (the ``*IDN?`` field,
        which is a ``"0"`` placeholder on firmware 3.4.6b1).
        ``metadata.serial_number`` is the canonical real SN —
        suitable for identity verification before destructive
        operations like :meth:`reset`.

        Closes daqifi-python-core#74.
        """
        return self._metadata

    def refresh_metadata(self, timeout: float = 2.0) -> DeviceMetadata:
        """Re-query ``SYSInfoPB?`` and refresh the cached metadata.

        Args:
            timeout: Seconds to wait for a parseable protobuf reply.

        Returns:
            The refreshed :class:`DeviceMetadata` (also accessible
            via :attr:`metadata`).

        Raises:
            CommandError: If no parseable protobuf message arrives
                within ``timeout`` seconds.
            DAQiFiConnectionError: If the device isn't connected.
        """
        # Use the public `is_connected` so a closed underlying serial
        # handle (e.g. after USB drop while `_connected` is still
        # True) surfaces as the documented `DAQiFiConnectionError`
        # instead of falling through to query_binary's `RuntimeError`.
        if not self.is_connected:
            raise DAQiFiConnectionError(
                "Cannot refresh metadata: device not connected"
            )
        from ._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage
        from .communication import ProtobufMessageParser

        try:
            raw = self._comm.query_binary(
                scpi_commands.get_device_info().data, timeout=timeout
            )
        except RuntimeError as e:
            # SerialInterface.query_binary raises RuntimeError on
            # closed-port / I/O failure; honor the documented
            # exception contract by re-raising as a connection error.
            raise DAQiFiConnectionError(
                f"Cannot refresh metadata: {e}"
            ) from e
        parser = ProtobufMessageParser(message_factory=DaqifiOutMessage.FromString)
        try:
            messages, _consumed = parser.parse_messages(raw)
        except Exception as e:
            # ProtobufMessageParser is documented to be resilient
            # (advances one byte on bad payloads), but defense-in-depth
            # against a future schema change or a parser bug — keep the
            # documented exception contract intact.
            raise CommandError(
                f"SYSInfoPB? returned unparseable bytes after {timeout}s "
                f"(received {len(raw)} bytes): {e}"
            ) from e
        if not messages:
            raise CommandError(
                f"SYSInfoPB? returned no parseable protobuf message after "
                f"{timeout}s (received {len(raw)} bytes)"
            )
        # Walk the parsed frames newest-to-oldest and pick the latest
        # one that actually carries SYSInfoPB-shaped metadata. If
        # background streaming is active when this query runs,
        # query_binary's buffer can contain stream frames AFTER the
        # SYSInfoPB reply; ``update_from_protobuf`` would no-op on
        # those (it only writes non-empty fields) and leave metadata
        # stale.
        #
        # The "metadata-shaped" check accepts any of the SystStatusPB-
        # specific fields (pn, sn, fw_rev, hw_rev, host_name, ssid)
        # being populated — a SYSInfoPB? response that omits pn/sn
        # but carries other identity fields is still a valid refresh
        # source, and stream frames carry none of these. The string
        # ``"0"`` is rejected explicitly because firmware 3.4.6b1
        # uses it as a placeholder in the *IDN? field, so a future
        # cross-wiring bug couldn't sneak it through here.
        metadata_msg = None
        for candidate in reversed(messages):
            if _frame_has_metadata_fields(candidate.data):
                metadata_msg = candidate.data
                break
        if metadata_msg is None:
            # Parseable frames arrived but none carried SYSInfoPB-
            # shaped metadata (e.g. only background-streaming
            # frames were in the buffer when SYSInfoPB? was lost on
            # the wire). Surface this as a CommandError rather than
            # returning silently — the cache stays unchanged, but
            # callers (especially destructive-op identity gates)
            # need to know the refresh didn't actually succeed.
            raise CommandError(
                f"SYSInfoPB? returned {len(messages)} parseable frame(s), "
                f"none of which carried SYSInfoPB metadata fields "
                f"(received {len(raw)} bytes; metadata cache unchanged)"
            )
        self._metadata.update_from_protobuf(metadata_msg)
        return self._metadata

    @property
    def firmware_version(self) -> str:
        """Get firmware version"""
        return self._device_info.get("firmware_version", "Unknown")

    @property
    def analog_outputs(self) -> AnalogOutputs:
        """
        Get analog output (DAC) channels.

        Returns:
            AnalogOutputs collection

        Raises:
            FeatureNotSupportedError: If device doesn't have DAC (non-NQ3)

        Example:
            # Set voltage; an output drives the voltage last set
            # (there is no DAC enable on current firmware)
            device.analog_outputs[0].voltage = 5.0
        """
        if self._analog_outputs is None:
            raise FeatureNotSupportedError(
                f"Analog outputs not available on {self._variant.value}. "
                "DAC is only available on NQ3 variant."
            )
        return self._analog_outputs

    @property
    def analog_inputs(self) -> AnalogInputs:
        """
        Get analog input (ADC) channels.

        Returns:
            AnalogInputs collection

        Example:
            # Configure channel
            device.analog_inputs[0].range = "10V"
            device.analog_inputs[0].enabled = True

            # Configure multiple channels
            device.analog_inputs.configure_channels([0, 1, 2], range="5V")
        """
        if self._analog_inputs is None:
            raise FeatureNotSupportedError("Analog inputs not available")
        return self._analog_inputs

    @property
    def digital_io(self) -> DigitalIO:
        """
        Get digital I/O channels.

        Returns:
            DigitalIO collection

        Example:
            # Configure as output and set high
            device.digital_io[0].direction = "output"
            device.digital_io[0].state = True

            # Configure as input and read
            device.digital_io[1].direction = "input"
            state = device.digital_io[1].state
        """
        if self._digital_io is None:
            raise FeatureNotSupportedError("Digital I/O not available")
        return self._digital_io

    @property
    def power_state(self) -> PowerState:
        """
        Get current power state.

        Returns:
            PowerState enum (STANDBY, POWERED_UP, or POWERED_UP_EXT_DOWN)

        Example:
            state = device.power_state
            if state == PowerState.POWERED_UP:
                print("Device fully powered")
        """
        try:
            response = self._comm.send_query(scpi_commands.get_power_state().data)
            state_val = scpi.parse_int(response)
            return PowerState(state_val)
        except Exception as e:
            raise CommandError(f"Failed to read power state: {e}")

    @power_state.setter
    def power_state(self, state: PowerState):
        """
        Set power state.

        Args:
            state: Target PowerState

        Example:
            device.power_state = PowerState.POWERED_UP
        """
        # Use send_set_with_error so the SYST:ERR? response captured
        # by the setter is the one we evaluate — re-querying after a
        # False return advances the device's error queue past the
        # actual entry and silently swallows it (#14 / Qodo PR #53).
        try:
            success, error_response = self._comm.send_command_with_error(
                scpi_commands.set_power_state(int(state)).data, delay=1.0,
            )
            check_setter_response(success, error_response, "set power state")
        except CommandError:
            raise
        except Exception as e:
            raise CommandError(f"Failed to set power state: {e}")

    def power_up(self):
        """
        Power up device to POWERED_UP state.

        Required for DAC operation (needs 10V rail).

        Example:
            device.power_up()
            device.analog_outputs[0].voltage = 5.0
        """
        self.power_state = PowerState.POWERED_UP

    def power_down(self):
        """
        Power down device to STANDBY state.

        Example:
            device.power_down()
        """
        self.power_state = PowerState.STANDBY

    def reset(self):
        """
        Reset device to default state (*RST command).

        Example:
            device.reset()
        """
        self._comm.reset()

    def get_system_info(self) -> str:
        """
        Get detailed system information.

        Returns:
            System info string from SYST:INFO? command

        Example:
            info = device.get_system_info()
            print(info)
        """
        try:
            return self._comm.send_query(scpi_commands.get_system_info().data)
        except Exception as e:
            raise CommandError(f"Failed to get system info: {e}")

    def get_last_error(self) -> str:
        """
        Get last error from device error queue.

        Returns:
            Error message or "No error"

        Example:
            error = device.get_last_error()
            if error != "No error":
                print(f"Device error: {error}")
        """
        try:
            return self._comm.send_query(scpi_commands.get_last_error().data)
        except Exception as e:
            raise CommandError(f"Failed to query error: {e}")

    def clear_errors(self):
        """
        Clear device error queue.

        Example:
            device.clear_errors()
        """
        self._comm.clear_errors()

    def drain_errors(
        self, max_iterations: int = 256, empty_budget: int = 3
    ) -> List[str]:
        """Pop ``SYST:ERR?`` entries from the device until the queue
        reports ``"No error"`` and return the popped entries.

        Counterpart to :meth:`get_last_error` (which pops one entry):
        this drains the whole queue and returns ownership of the
        popped entries to the caller — log them, surface them in a
        health-check report, throw on hardware faults, or discard if
        known-stale. Mirrors upstream
        ``DaqifiDevice.DrainErrorQueueAsync`` (daqifi-core PR #185).

        Args:
            max_iterations: Safety cap on the number of ``SYST:ERR?``
                queries. Defaults to 256 — large enough to drain a
                deeply queued device, small enough that a runaway loop
                is bounded.
            empty_budget: Number of consecutive empty/unparseable
                replies tolerated before bailing. Defaults to 3 (~0.3 s
                worst-case stall vs ~25 s with the unconditional cap).

        Returns:
            The list of error strings popped from the queue. Empty
            when the queue was already clean. May contain a synthetic
            ``DRAIN_INCONCLUSIVE`` sentinel if drain exited without
            seeing ``0,"No error"`` — caller can detect "queue not
            confirmed clean."

        Raises:
            ValueError: ``max_iterations`` or ``empty_budget`` ≤ 0.
            CommandError: transport-level failure during the drain
                (matches the pattern used by :meth:`get_last_error`).
        """
        # Validate before the try/except so ValueError propagates
        # cleanly instead of getting wrapped into CommandError.
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if empty_budget <= 0:
            raise ValueError("empty_budget must be positive")
        try:
            return self._comm.drain_errors(
                max_iterations=max_iterations, empty_budget=empty_budget
            )
        except Exception as e:
            # Preserve original exception context for debugging
            # transport-level failures.
            raise CommandError(f"Failed to drain error queue: {e}") from e

    # ==========================================================================
    # SD Card File Management
    # ==========================================================================

    def sd_enable(self, enable: bool = True):
        """
        Enable or disable SD card.

        Args:
            enable: True to enable, False to disable

        Example:
            device.sd_enable(True)
        """
        msg = scpi_commands.enable_storage_sd() if enable else scpi_commands.disable_storage_sd()
        success, error_response = self._comm.send_command_with_error(msg.data)
        check_setter_response(success, error_response, f"set SD enabled={enable}")

    def sd_list_files(self, directory: str = "") -> List[str]:
        """
        List files on SD card.

        Args:
            directory: Optional directory path (default: root/DAQiFi)

        Returns:
            List of file paths

        Example:
            files = device.sd_list_files()
            for f in files:
                print(f)
        """
        if directory:
            cmd = scpi_commands.get_sd_file_list_for_directory(directory)
        else:
            cmd = scpi_commands.get_sd_file_list()
        response = self._comm.send_command(cmd.data)

        # Parse file list from response (format: path,size per line)
        files = []
        self._file_sizes = {}  # Store for later use
        for line in response.strip().split('\n'):
            line = line.strip()
            # Skip empty lines and prompts
            if line and not line.startswith('DAQIFI') and not line.startswith('#'):
                parts = line.split()
                if len(parts) >= 2 and parts[-1].isdigit():
                    # New format: path size
                    path = ' '.join(parts[:-1])  # Rejoin in case path has spaces
                    size = int(parts[-1])
                    files.append(path)
                    self._file_sizes[path] = size
                else:
                    # Old format: just path
                    files.append(line)
                    self._file_sizes[line] = 0  # Unknown size
        return files

    def sd_get_file_size(self, filename: str) -> int:
        """Get cached file size from last LIST command."""
        return self._file_sizes.get(filename, 0)

    def sd_get_file(self, filename: str, timeout: float = 30.0) -> str:
        """
        Retrieve file contents from SD card.

        Args:
            filename: Name of file to retrieve
            timeout: Maximum time to wait for file transfer (default: 30s)

        Returns:
            File contents as string

        Example:
            data = device.sd_get_file("test.csv")
            print(data)
        """
        import time

        if not self._comm.is_open():
            raise RuntimeError("Serial port not open")

        # Send GET command
        cmd = scpi_commands.get_sd_file(filename).data
        cmd_bytes = (cmd + "\r").encode('ascii')
        self._comm._serial.write(cmd_bytes)
        self._comm._serial.flush()

        # Read until we see __END_OF_FILE__ marker (simplified, no CRLF)
        response = b""
        start_time = time.time()
        end_marker = b"__END_OF_FILE__"

        while (time.time() - start_time) < timeout:
            if self._comm._serial.in_waiting > 0:
                chunk = self._comm._serial.read(self._comm._serial.in_waiting)
                response += chunk

                # Check if we've received the end marker
                if end_marker in response:
                    break

                # Reset start time when receiving data (prevents timeout during large transfers)
                start_time = time.time()
            else:
                # No data available, wait a bit
                time.sleep(0.01)

        # Decode response
        response_str = response.decode('ascii', errors='ignore')

        # Remove echoed command
        if cmd in response_str:
            response_str = response_str.replace(cmd, "")

        # Remove DAQIFI> prompt
        response_str = response_str.replace("DAQIFI>", "")

        # Remove end marker
        response_str = response_str.replace("__END_OF_FILE__", "")

        # Clean up
        response_str = response_str.strip()

        return response_str

    def sd_get_file_binary(self, filename: str, timeout: float = 30.0) -> bytes:
        """
        Retrieve raw binary file contents from SD card.

        Unlike sd_get_file(), this preserves all byte values (no ASCII
        decoding) for binary integrity verification.

        Args:
            filename: Name of file to retrieve
            timeout: Maximum time to wait for file transfer (default: 30s)

        Returns:
            File contents as raw bytes

        Raises:
            TimeoutError: If __END_OF_FILE__ marker not received within timeout
            RuntimeError: If serial port not open

        Example:
            data = device.sd_get_file_binary("benchmark.dat")
            assert len(data) == expected_size
        """
        import time

        if not self._comm.is_open():
            raise RuntimeError("Serial port not open")

        # Send GET command
        cmd = scpi_commands.get_sd_file(filename).data
        cmd_bytes = (cmd + "\r").encode('ascii')
        self._comm._serial.write(cmd_bytes)
        self._comm._serial.flush()

        # Read until we see __END_OF_FILE__ marker
        # Use bytearray for O(n) accumulation instead of O(n^2) bytes concat
        response = bytearray()
        end_marker = b"__END_OF_FILE__"
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._comm._serial.in_waiting > 0:
                chunk = self._comm._serial.read(self._comm._serial.in_waiting)
                response.extend(chunk)

                if end_marker in response:
                    break
            else:
                time.sleep(0.01)
        else:
            raise TimeoutError(
                f"SD file download timed out after {timeout}s "
                f"({len(response)} bytes received, no EOF marker)")

        # Strip leading echo and prompt from raw bytes
        # Echo format: <cmd>\r\nDAQIFI> <file data>__END_OF_FILE__...
        # Only search the first 200 bytes to avoid truncating binary data
        # that might coincidentally contain the prompt string
        header = bytes(response[:200])
        for marker in (b"DAQIFI> ", b"DAQIFI>"):
            echo_end = header.find(marker)
            if echo_end >= 0:
                response = response[echo_end + len(marker):]
                break

        # Strip EOF marker
        eof_idx = response.find(end_marker)
        if eof_idx >= 0:
            response = response[:eof_idx]

        return bytes(response)

    def sd_delete_file(self, filename: str):
        """
        Delete a file from SD card.

        Args:
            filename: Name of file to delete

        Example:
            device.sd_delete_file("old_data.csv")
        """
        success, error_response = self._comm.send_command_with_error(
            scpi_commands.delete_sd_file(filename).data
        )
        check_setter_response(success, error_response, f"delete SD file {filename!r}")

    def sd_format(self):
        """
        Format SD card (erase all files).

        WARNING: This will erase ALL files on the SD card!

        Example:
            device.sd_format()  # Careful!
        """
        success, error_response = self._comm.send_command_with_error(
            scpi_commands.format_sd_card().data
        )
        check_setter_response(success, error_response, "format SD card")

    def sd_set_logging(self, filename: str):
        """
        Set filename for SD card logging.

        Args:
            filename: Name of file to log data to

        Example:
            device.sd_enable(True)
            device.sd_set_logging("data.csv")
            # Now streaming will log to SD card
        """
        success, error_response = self._comm.send_command_with_error(
            scpi_commands.set_sd_logging_file_name(filename).data
        )
        check_setter_response(success, error_response, f"set SD logging file {filename!r}")

    def sd_benchmark(self, size_kb: int = 1024) -> dict:
        """
        Run SD card write speed benchmark.

        Args:
            size_kb: Size of test data in KB (default 1024 = 1MB)

        Returns:
            Dictionary with benchmark results

        Example:
            results = device.sd_benchmark(1024)
            print(f"Speed: {results['speed_mbps']:.2f} MB/s")
        """
        # Run benchmark
        self._comm.send_command(scpi_commands.run_sd_benchmark(size_kb).data)

        # Query results
        response = self._comm.send_command(scpi_commands.get_sd_benchmark_results().data)
        parts = response.strip().split(',')

        if len(parts) == 3:
            bytes_written = int(parts[0])
            time_ms = int(parts[1])
            speed_bps = int(parts[2])

            return {
                'bytes_written': bytes_written,
                'time_ms': time_ms,
                'speed_bps': speed_bps,
                'speed_mbps': speed_bps / (1024 * 1024)
            }
        else:
            return {'error': 'Could not parse benchmark results'}

    # ==========================================================================
    # Streaming
    # ==========================================================================

    def create_stream(
        self,
        channels: List[int],
        sample_rate: int = 1000,
        format: int = 1,
        async_mode: bool = False
    ) -> Union[StreamManager, AsyncStreamManager]:
        """
        Create a stream manager for data acquisition.

        Args:
            channels: List of channel indices to stream
            sample_rate: Sample rate in Hz (default 1000)
            format: Streaming format - 0=ProtoBuf, 1=JSON (default), 2=CSV
            async_mode: If True, return AsyncStreamManager for async/await

        Returns:
            StreamManager or AsyncStreamManager

        Raises:
            ConfigurationError: If invalid channels specified

        Example:
            # Blocking mode with JSON (default)
            stream = device.create_stream(channels=[0, 1], sample_rate=1000)
            stream.start()
            samples = stream.read(num_samples=100)
            stream.stop()

            # CSV format
            stream = device.create_stream(channels=[0, 1], format=2)
            stream.start()
            samples = stream.read(num_samples=100)
            stream.stop()

            # Callback mode
            def on_sample(sample):
                print(sample)
            stream = device.create_stream(channels=[0, 1])
            stream.start(callback=on_sample)

            # Context manager
            with device.create_stream(channels=[0, 1]) as stream:
                samples = stream.read(num_samples=100)

            # Async mode
            async with device.create_stream([0, 1], async_mode=True) as stream:
                samples = await stream.read(100)
        """
        # Validate channels
        max_channels = len(self.analog_inputs._channels) if self._analog_inputs else 0
        for ch in channels:
            if ch < 0 or ch >= max_channels:
                raise ConfigurationError(
                    f"Invalid channel {ch}. Valid range: 0-{max_channels-1}"
                )

        dispatch = self._build_per_channel_dispatch()

        # Create appropriate stream manager
        if async_mode:
            return AsyncStreamManager(
                self._comm, channels, sample_rate, format,
                per_channel_dispatch=dispatch,
            )
        else:
            return StreamManager(
                self._comm, channels, sample_rate, format,
                per_channel_dispatch=dispatch,
            )

    def _build_per_channel_dispatch(self):
        """Return a callable the StreamManager will invoke per channel
        per produced :class:`Sample` so per-channel ``SampleReceived``
        handlers fire automatically (closes #107).

        The returned closure is intentionally tolerant: indices outside
        the analog-input range are skipped silently (StreamManager
        already validated channel indices at start_streaming time, but
        the dispatch path runs in the streaming thread where partially-
        formed samples shouldn't crash the loop).
        """
        from datetime import datetime, timezone

        def _dispatch(channel_index: int, value: float, timestamp_seconds: float) -> None:
            if self._analog_inputs is None:
                return
            if not (0 <= channel_index < len(self._analog_inputs)):
                return
            try:
                ts = datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                # A malformed timestamp (e.g. a JSON/CSV decode glitch
                # producing an out-of-range float) must not poison the
                # streaming thread. Skip this dispatch.
                return
            self._analog_inputs[channel_index].set_active_sample(
                DataSample(timestamp=ts, value=value)
            )

        return _dispatch

    def __enter__(self):
        """
        Context manager entry.

        Example:
            with NyquistDevice.connect("/dev/ttyACM0") as device:
                device.power_up()
                device.analog_outputs[0].voltage = 5.0
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - automatically disconnect"""
        self.disconnect()

    def __repr__(self) -> str:
        if self.is_connected:
            return f"NyquistDevice(variant={self._variant.value}, port={self._comm.port}, connected=True)"
        return f"NyquistDevice(port={self._comm.port}, connected=False)"

    def __str__(self) -> str:
        if self.is_connected:
            return f"{self._variant.value} at {self._comm.port}"
        return f"Disconnected NyquistDevice"
