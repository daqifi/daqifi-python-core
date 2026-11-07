"""
Main device controller for DAQiFi Nyquist devices.

Provides high-level API for device interaction.
"""

from typing import Dict, Any, Optional, Union, List
from ._internal.comm import DataSample, SerialInterface, PowerState, DeviceMetadata
import time

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
from .sdcard import END_OF_FILE_MARKER, TRANSFER_ERROR_MARKER
from .exceptions import (
    ConnectionError as DAQiFiConnectionError,
    CommandError,
    ConfigurationError,
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
        # Sizes from the last sd_list_files(); empty until one runs.
        # ⛔ Optional[int]: None means "that listing did not state a
        # size" (the old listing format), which is NOT the same fact as
        # a listed 0, an empty file. A size is an enforced transfer
        # boundary now, so conflating them rejects every download of an
        # unsized entry.
        self._file_sizes: Dict[str, Optional[int]] = {}
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
            cause = getattr(self._comm, "last_open_error", None)
            if cause is not None:
                # A SerialPortConnectError (a DAQiFiConnectionError) that
                # names the cause: not found, in use or access denied.
                raise cause
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
        # A listing from this session is not a safe download length for
        # the next one: the card may change while disconnected. Same
        # reasoning as DaqifiStreamingDevice.disconnect.
        self._forget_sd_file_sizes()
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
        # Can change what is on the card, so a listed size is no
        # longer a safe download boundary (see _forget_sd_file_sizes).
        self._forget_sd_file_sizes()
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
        lines = response.strip().split('\n')

        # The firmware ends a listing with "__END_OF_LIST__ <status>"; a
        # listing it marked INCOMPLETE or FAILED isn't the whole card (#188).
        from .exceptions import SdCardListIncompleteException
        from .sdcard import _list_end_status, sd_card_listing_status

        status = sd_card_listing_status(lines)
        if status in ("incomplete", "failed"):
            raise SdCardListIncompleteException(
                [ln.strip() for ln in lines],
                f"the device marked the listing {status}",
            )

        # Parse file list from response (format: path,size per line)
        files = []
        self._file_sizes = {}  # Store for later use
        for line in lines:
            line = line.strip()
            if _list_end_status(line) is not None:
                continue
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
                    # Old format: just path, no size. ⛔ STORED AS None, NOT
                    # 0. These are two different facts -- "this file is
                    # empty" and "this listing did not say" -- and a size is
                    # now an enforced transfer boundary, so conflating them
                    # makes every download of an unsized entry fail against
                    # a zero-byte boundary. 0 meant "unknown" here only
                    # because nothing read it.
                    files.append(line)
                    self._file_sizes[line] = None
        return files

    def sd_get_file_size(self, filename: str) -> int:
        """Get cached file size from last LIST command (0 if the file wasn't
        in it, or no listing has run yet).

        ⚠ Also 0 after anything that can change the card — see
        :meth:`_forget_sd_file_sizes`. A size from before a recording is not
        a size.
        """
        return self._listed_size_for(filename) or 0

    def _listed_size_for(self, filename: str) -> Optional[int]:
        """The last listing's size for ``filename``, or ``None`` when it is
        not known UNAMBIGUOUSLY.

        ⛔ EXACT KEY ONLY, and both softer rules were tried and removed.
        A listing stores what the device PRINTED -- ``DAQiFi/log.bin`` --
        while a download is asked for by the name the caller holds, usually
        the bare ``log.bin``, so an exact lookup misses for a path-prefixed
        listing and the download runs unsized. That is the safe direction.

        Matching on the final path segment closed that gap and opened a
        worse one: it BORROWS ACROSS DIRECTORIES. With ``OTHER/log.bin``
        listed at 3 bytes, a download of ``log.bin`` took 3 as its enforced
        boundary and a valid 6-byte transfer was rejected. Joining the name
        to a listed directory fails the same way and for the same reason:
        the device resolves a bare GET in its OWN default directory, not in
        whichever one happened to be listed last. Neither is done.

        ⚠ A listed 0 is an EMPTY FILE and is returned as 0. "That listing
        did not state a size" is a different fact and is stored as ``None``
        by :meth:`sd_list_files`, which is what makes the ``is not None``
        test below safe -- an ``if size:`` sent an empty file down the
        unsized path and, in the basename revision, let it borrow another
        entry's boundary.
        """
        if filename not in self._file_sizes:
            return None
        # `is not None`, never truthiness: a listed size of 0 is a real,
        # empty file and must stay distinguishable from "this listing did
        # not say", which is stored as None. An `if size:` here sent an
        # empty file down the unsized path and, in an earlier revision that
        # also matched basenames, let it borrow another directory's size.
        size = self._file_sizes[filename]
        return size if size is not None else None

    def _forget_sd_file_sizes(self) -> None:
        """Drop the cached listing sizes.

        ⛔ CALLED FROM EVERY OPERATION THAT CAN CHANGE THE CARD, because a
        cached size became load-bearing the moment the download started
        enforcing it: a listing taken before more data was logged rejects a
        perfectly good transfer with ``OSError``. The base ignored these
        numbers entirely, so staleness cost nothing there.

        ⭐ THE COSTS ARE ASYMMETRIC, which is why this forgets everything
        rather than tracking what changed. Over-forgetting loses only the
        marker-in-content discrimination and degrades to the unsized path
        the base always used; under-forgetting raises on valid data. When in
        doubt, forget.

        Mirrors ``DaqifiStreamingDevice._forget_sd_card_sizes``, which does
        the same for the modern path -- including on disconnect, since a
        listing from an earlier session is not a safe length either.
        """
        self._file_sizes = {}

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

        Raises:
            SdCardTransferErrorException: the device reported a read error
                part-way through, ending the transfer with
                ``__TRANSFER_ERROR__``. Previously this path returned the
                partial content as if it were the whole file.
            TimeoutError: no terminal marker arrived within ``timeout``.
            RuntimeError: the serial port is not open.
        """
        if not self._comm.is_open():
            raise RuntimeError("Serial port not open")

        # Send GET command
        cmd = scpi_commands.get_sd_file(filename).data
        cmd_bytes = (cmd + "\r").encode('ascii')
        self._comm._serial.write(cmd_bytes)
        self._comm._serial.flush()

        # ⛔ SAME RECEIVE AS THE BINARY SIBLING, and the sweeps that used to
        # be here are gone with it. This path removed the command text, the
        # prompt and the marker with `.replace()`, which deletes EVERY
        # occurrence at ANY offset -- so a payload legitimately containing
        # any of the three was corrupted throughout, not merely at the head
        # -- and then `.strip()`ed the result, which is no part of the
        # transfer contract and ate the file's own leading and trailing
        # whitespace.
        #
        # ⚠ The decode is deliberately unchanged, including
        # `errors="ignore"`. Making it strict would raise on logs this
        # accepts today, and choosing another encoding would change what
        # this returns; both are a separate decision from the receive
        # contract, so they are not bundled in here.
        # ⚠ idle_reset=True restores THIS path's original timeout semantics.
        # The base reset its clock on every chunk, so the timeout measured
        # SILENCE; delegating made it absolute, and a slow but progressing
        # text download then died at the 30 s default. The binary sibling
        # was absolute in the base and stays absolute.
        return self._receive_sd_file(
            filename, cmd, timeout, idle_reset=True,
        ).decode("ascii", errors="ignore")

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
            SdCardTransferErrorException: the device reported a read error
                part-way through, ending the transfer with
                ``__TRANSFER_ERROR__`` rather than ``__END_OF_FILE__``.
            TimeoutError: If __END_OF_FILE__ marker not received within timeout
            RuntimeError: If serial port not open

        Example:
            data = device.sd_get_file_binary("benchmark.dat")
            assert len(data) == expected_size
        """
        if not self._comm.is_open():
            raise RuntimeError("Serial port not open")

        # Send GET command
        cmd = scpi_commands.get_sd_file(filename).data
        cmd_bytes = (cmd + "\r").encode('ascii')
        self._comm._serial.write(cmd_bytes)
        self._comm._serial.flush()

        return self._receive_sd_file(filename, cmd, timeout, idle_reset=False)

    #: How long the capture waits for more data once a terminal marker has
    #: been seen but the expected-size target has not been reached. This is
    #: what makes a stale LARGER size cost a fraction of a second instead of
    #: the whole timeout: the marker is already in hand, so the only question
    #: is whether the rest of the promised bytes are still coming.
    _SD_MARKER_GRACE_S = 0.3

    def _read_sd_response(
        self, timeout: float, idle_reset: bool, target: Optional[int],
        echo: bytes = b"",
    ) -> bytes:
        """Read the GET response off the port. Transport only -- what the
        bytes MEAN is the receiver's job.

        ⛔ THE STOP RULE IS SIZE-AWARE, and it has to be. Stopping at the
        first terminal marker would truncate exactly the case the listed
        size exists to decide: a file whose own content contains
        ``__END_OF_FILE__``. So with a size in hand this reads until the
        promised bytes plus a marker have arrived; without one, the first
        marker ends it.

        ⚠ And a marker plus a quiet line ends it either way, after
        ``_SD_MARKER_GRACE_S``. That is the stale-LARGER-size case: the
        promised bytes are never coming, and waiting out the full timeout
        for them would turn a stale cache into a 30-second stall.

        ``idle_reset`` restores the text path's original semantics, where
        the timeout measures SILENCE rather than total duration, so a slow
        but progressing download is not cut off. The binary path's timeout
        was absolute and stays that way.
        """
        serial = self._comm._serial
        out = bytearray()
        deadline = time.monotonic() + timeout
        quiet_since: Optional[float] = None
        while time.monotonic() < deadline:
            pending = serial.in_waiting
            if pending:
                out.extend(serial.read(pending))
                quiet_since = None
                if idle_reset:
                    deadline = time.monotonic() + timeout
            # ⛔ NEVER SCAN THE ECHO FOR A TERMINAL MARKER. A GET names a
            # file, and a file can be named `__TRANSFER_ERROR__.bin` or
            # `__END_OF_FILE__`, so the echoed command can contain EITHER
            # marker's bytes -- stopping on that ends the capture on the
            # echo and loses the payload. (An earlier revision of this loop
            # scanned the whole buffer and broke an existing pin for the
            # first of those names; a comment then claimed the second could
            # not happen, which the audit's own defect-4 input disproves.)
            #
            # So the scan starts after the echo when the echo is complete,
            # and waits when the echo is still arriving. With echo off there
            # is no echo and the payload starts at 0, which this also gets
            # right.
            buf = bytes(out)
            if buf.startswith(echo):
                region = buf[len(echo):]
            elif echo.startswith(buf):
                region = b""            # the echo is still coming in
            else:
                region = buf            # echo is off, or something else
            eof_present = END_OF_FILE_MARKER in region
            err_present = TRANSFER_ERROR_MARKER in region
            if eof_present and (target is None or len(out) >= target):
                break
            if (eof_present or err_present) and quiet_since is not None:
                # A terminal marker in hand and the line gone quiet. Either
                # the transfer ended with an error marker, or a stale LARGER
                # listed size promised bytes that are not coming. Both are
                # the receiver's to interpret; waiting out the full timeout
                # for them would turn a stale cache into a 30-second stall.
                if time.monotonic() - quiet_since >= self._SD_MARKER_GRACE_S:
                    break
            if not pending:
                if quiet_since is None:
                    quiet_since = time.monotonic()
                time.sleep(0.01)
        return bytes(out)

    def _receive_sd_file(
        self, filename: str, cmd: str, timeout: float, idle_reset: bool,
    ) -> bytes:
        """The SD receive contract, applied by the one implementation of it.

        ⛔ THIS USED TO BE A SECOND COPY OF THAT CONTRACT, and the copy is
        what #154 and #206 item 5 are: it knew only ``__END_OF_FILE__``, so
        a failed transfer waited out the whole timeout and could not be told
        from an empty file; it ended the read at the first marker ANYWHERE,
        so a file containing those bytes truncated itself; and it found the
        prompt by searching the first 200 bytes, so a file containing
        ``DAQIFI>`` lost its head. ``SdCardFileReceiver`` already decided all
        three, including the hard one -- telling the device's
        ``__TRANSFER_ERROR__`` from file content that happens to contain it,
        by what starts AT the size boundary.

        The size comes from the last listing when there was one. Without it
        the first marker still ends the file, which is the documented
        limitation of an unsized transfer rather than anything this adds.
        """
        import io

        from .sdcard import SdCardFileReceiver

        size = self._listed_size_for(filename)
        target = None if size is None else size + len(END_OF_FILE_MARKER)
        raw = self._read_sd_response(
            timeout, idle_reset, target, echo=cmd.encode("ascii"),
        )
        # The command as sent, because the console may echo it back ahead of
        # the payload. Removed as a prefix, and before any marker
        # classification -- the echo of a GET for a file named
        # "__TRANSFER_ERROR__.bin" carries that marker's own bytes, and
        # classifying first reports a working transfer as a device read
        # error.
        preamble = cmd.encode("ascii")

        def interpret(expected_size: Optional[int]) -> bytes:
            destination = io.BytesIO()
            SdCardFileReceiver(io.BytesIO(raw)).receive(
                destination, filename,
                timeout_s=max(timeout, 0.001),
                expected_size=expected_size,
                preamble=preamble,
            )
            return destination.getvalue()

        if size is not None:
            try:
                return interpret(size)
            except (IOError, TimeoutError):
                # ⛔ ON THIS PATH THE LISTED SIZE IS A HINT, NOT A BOUNDARY.
                # The base ignored it entirely, so a disagreement between a
                # cached size and the wire has never been an error here --
                # and making it one turned every stale-cache path into an
                # OSError on a VALID download. Eight such paths were found
                # across two audit rounds, twice after I had claimed the
                # list was complete, so the fault space is open and
                # removing the consequence beats chasing the paths.
                #
                # A correct size still buys the marker-in-content
                # discrimination; a wrong one now costs only that. The
                # bytes are already in memory, so this re-reading is free:
                # no extra wire time, nothing re-sent to the device.
                #
                # ⚠ SdCardTransferErrorException is NOT caught: it is not an
                # OSError, and a device-reported read error is a fact about
                # the transfer rather than a disagreement about its length.
                pass
        return interpret(None)

    def sd_delete_file(self, filename: str):
        """
        Delete a file from SD card.

        Args:
            filename: Name of file to delete

        Example:
            device.sd_delete_file("old_data.csv")
        """
        # Can change what is on the card, so a listed size is no
        # longer a safe download boundary (see _forget_sd_file_sizes).
        self._forget_sd_file_sizes()
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
        # Can change what is on the card, so a listed size is no
        # longer a safe download boundary (see _forget_sd_file_sizes).
        self._forget_sd_file_sizes()
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
        # Names the file a following recording appends to, and the legacy
        # API has no separate "start logging" step that would invalidate
        # later. Forgetting costs a listing; keeping costs a rejected
        # download (see _forget_sd_file_sizes).
        self._forget_sd_file_sizes()
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
        # Can change what is on the card, so a listed size is no
        # longer a safe download boundary (see _forget_sd_file_sizes).
        self._forget_sd_file_sizes()
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
        async_mode: bool = False,
        integer_units: Optional[str] = None,
    ) -> Union[StreamManager, AsyncStreamManager]:
        """
        Create a stream manager for data acquisition.

        Args:
            channels: List of channel indices to stream
            sample_rate: Sample rate in Hz (default 1000)
            format: Streaming format - 0=ProtoBuf, 1=JSON (default), 2=CSV
            async_mode: If True, return AsyncStreamManager for async/await
            integer_units: What integer JSON/CSV values mean ("millivolts"
                at voltage precision 0, "raw" in raw mode); see
                :class:`StreamManager`. Decimal values are volts either way.

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
        if max_channels == 0:
            raise ConfigurationError(
                "This device has no analog inputs to stream (not connected, or "
                f"its board variant is {self._variant.name})."
            )
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
                integer_units=integer_units,
                # ⛔ THE EIGHTH SD WRITER, and the one an enumeration of
                # sd_-named methods could not see: streaming to the card
                # changes what is on it. The manager holds the transport,
                # not this object, so it invalidates through this callback.
                on_recording_change=self._forget_sd_file_sizes,
            )
        else:
            return StreamManager(
                self._comm, channels, sample_rate, format,
                per_channel_dispatch=dispatch,
                integer_units=integer_units,
                on_recording_change=self._forget_sd_file_sizes,
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
