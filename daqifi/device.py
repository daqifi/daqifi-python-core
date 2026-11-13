"""
Main device controller for DAQiFi Nyquist devices.

Provides high-level API for device interaction.
"""

import threading
from typing import Callable, Dict, Any, Optional, Union, List
from ._internal.comm import (DataSample, SerialInterface, PowerState, DeviceMetadata,
                             SEND_DRAIN_TIMEOUT_S, drain_output_bounded)
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


# ⛔ A LIVENESS BOUND, NOT A TRANSPORT TIMEOUT. The legacy text path captures
# the whole response first and then re-reads it from an in-memory buffer that
# cannot block, so there is nothing there for a transport timeout to measure.
# This exists only so a defect in the parser cannot hang a caller forever, and
# it is deliberately NOT derived from the caller's timeout: doing that turned a
# completed 8 MiB transfer into a TimeoutError whenever the local parse
# outlasted the transport allowance (#257 r5, device.py:1009).
_LOCAL_REPARSE_LIVENESS_S = 3600.0

_METADATA_FIELD_NAMES = (
    "device_pn",
    "device_sn",
    "device_fw_rev",
    "device_hw_rev",
    "host_name",
    "ssid",
)


def _require_finite_sd_timeout(timeout: Any, caller: str) -> float:
    """Reject a non-finite SD download timeout as a USAGE error.

    ⛔ RULING HW A. `_read_sd_response`'s loop is `while time.monotonic() <
    deadline`, and the head-resolution wait it contains has no other bound: a
    payload that is a proper PREFIX of the echoed command keeps `head_span`
    answering None, so the marker scan never runs. With a finite timeout that
    costs the timeout and then returns the CORRECT bytes. With `inf` the loop
    cannot terminate at all, and with `nan` every comparison is False so it
    returns EMPTY immediately -- a silent wrong answer, which is the worse of
    the two.

    ⭐ A USAGE ERROR RATHER THAN A CLAMP. Substituting some finite value would
    be this module inventing a deadline the caller did not ask for, and the
    caller cannot tell it happened. `inf` is not a slow download, it is a
    request this transport cannot honour, so it is refused by name.

    ⛔ CALLED BEFORE THE GET IS WRITTEN, which is the whole point of its
    position. Both entries write the command and only then read, so a check
    placed after the write would refuse the call having ALREADY set the device
    streaming a file nobody is going to read -- a refusal that leaves the bench
    worse off than no check.

    ⚠ ZERO AND NEGATIVE ARE DELIBERATELY NOT REJECTED HERE. They terminate --
    the loop simply does not run -- so they are not this ruling's subject, and
    bundling them in would change behaviour for callers who pass 0 to mean
    "poll once" without anyone having asked for that. Scope is the non-finite
    case; see #260 for the real fix to the underlying ambiguity.
    """
    try:
        t = float(timeout)
    except (TypeError, ValueError):
        raise ValueError(
            "%s: timeout must be a finite number of seconds, got %r"
            % (caller, timeout)
        )
    # NaN is its own test because `nan != nan`; `inf` is caught by the bound.
    if t != t or t in (float("inf"), float("-inf")):
        raise ValueError(
            "%s: timeout must be FINITE, got %r. An SD download ends at a "
            "terminal marker, and the host cannot tell a payload that is a "
            "proper prefix of the echoed command from framing still in "
            "flight -- so with a non-finite timeout the read has no bound at "
            "all. Pass a real deadline (the default is 30.0). See daqifi "
            "python-core #260 for the out-of-band fix." % (caller, timeout)
        )
    return t


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
        # Optional[int]: None means "that listing did not state a size"
        # (the old listing format), a different fact from a listed 0, an
        # empty file. ⚠ Read ONLY by sd_get_file_size() now -- the download
        # stopped using it under ruling FO A2, so a stale entry costs a
        # wrong answer from that getter and nothing else. It used to be an
        # enforced transfer boundary, which is what made staleness fatal.
        self._file_sizes: Dict[str, Optional[int]] = {}
        # ⛔ r10 :808 -- INVALIDATION GENERATION. Bumped by
        # _forget_sd_file_sizes; a listing captures it before its query and
        # publishes only if it has not moved. Without it the atomic publish
        # r10 introduced could RESURRECT sizes an invalidation had already
        # dropped (see sd_list_files).
        self._file_sizes_generation = 0
        # ⛔ r11 :835 -- AND THE COMPARE AND THE PUBLISH MUST BE ONE STEP.
        # r11 wrote `if gen == captured: self._file_sizes = sizes`, which is
        # itself a CHECK-THEN-ACT: an invalidation landing between the
        # comparison and the assignment was observed (deterministically, with
        # a line-trace pause, and reachable through ordinary bytecode
        # switching) to publish stale sizes anyway. Fixing a check-then-act
        # with another check-then-act is the shape to watch for.
        #
        # ⭐ THIS LOCK IS A LEAF, which is what makes its ordering trivially
        # safe: it is held only around the bump-and-clear and the
        # compare-and-publish -- a handful of attribute writes -- and NEVER
        # across I/O, a device command, or a call that could acquire anything
        # else. So no acquirer can deadlock against it whatever else it holds,
        # and there is no ordering rule for callers to get wrong. (Checked:
        # no existing lock guards `_file_sizes`; `NyquistDevice` had none at
        # all, and the ones in channels/communication/streaming guard other
        # state. StreamManager fires its recording-change callback OUTSIDE its
        # own `_lock`, so even a non-leaf design would not have nested there --
        # but the leaf property does not depend on that staying true.)
        self._file_sizes_lock = threading.Lock()
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
    def connect(cls, port_or_info: Union[str, DeviceInfo], baudrate: int = 115200,
                verify: Optional[Callable[[Any], bool]] = None) -> 'NyquistDevice':
        """
        Connect to a Nyquist device.

        Args:
            port_or_info: Serial port path (str) or DeviceInfo from discovery
            baudrate: Serial baud rate (default 115200)
            verify: Optional callable run on the freshly opened port BEFORE
                this library writes anything, so a caller can confirm the
                device is the one it meant to address and refuse without
                touching it (#259). It receives the live handle and may read
                AND write — `*IDN?` is a write, and the caller's own
                verification bytes are the point; what is guaranteed is that
                the library writes nothing of its own first.

                Return truthy to proceed. A falsy return raises
                DeviceVerificationError; an exception from the callable is
                re-raised unchanged. Either way the port is CLOSED and the
                device is left untouched by this library.

                Without it there is no write-free way to find out which board
                is on a path: open() sends `SYSTem:ECHO -1` before any caller
                can read `*IDN?`. Verifying over a separate port first leaves
                a window between that port's close and the first byte to the
                reopened one — measured at 186.5 ms on one bench, most of it
                open()'s own settle — in which the path can be reassigned.
                Passing a callable here removes the window entirely, because
                verification and the first write happen on the same handle.

        Returns:
            Connected NyquistDevice instance

        Raises:
            ConnectionError: If connection fails
            DeviceVerificationError: If `verify` returned falsy

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
        # Set like cancel_event is — an attribute consulted inside open() —
        # so no constructor signature changes and the hook reaches the one
        # place that can honour it: before the first library write.
        device._comm.verify = verify
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
        # Can change what is on the card, so a listed size is no longer a
        # safe answer from sd_get_file_size() (see _forget_sd_file_sizes).
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
        # ⛔ r10 :808 -- CAPTURED BEFORE THE QUERY, compared at the publish.
        # Taken here rather than after the parse so it covers the whole window
        # in which an invalidation could arrive: the wire exchange is the long
        # part, and an invalidation during it is exactly the case the audit
        # reproduced.
        generation_at_entry = self._file_sizes_generation
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
        # ⛔ r9 :830, the same class at the WRITING end: BUILD LOCALLY, PUBLISH
        # ONCE. This used to assign `self._file_sizes = {}` here and then write
        # into `self._file_sizes[...]` per line -- one attribute read per
        # write. A concurrent `_forget_sd_file_sizes` (StreamManager's
        # recording-change callback, on another thread) REPLACES the attribute,
        # so writes before the replacement landed in a dict nobody holds any
        # more and writes after it landed in the one the forget had just
        # emptied: a cache left HALF POPULATED, which is a wrong answer from
        # sd_get_file_size rather than a crash and so would not have shown up
        # as a failure anywhere.
        #
        # ⛔⛔ r10 :808 -- AND THE INVALIDATION WINS, WHICH IS THE OPPOSITE OF
        # WHAT THIS COMMENT USED TO CLAIM. The earlier text said the
        # invalidation was "deliberately allowed to lose" because "a listing
        # that completes after it holds strictly newer data". THAT IS FALSE,
        # and the audit demonstrated it: thread A parses a listing and pauses
        # before publishing, thread B calls disconnect() which clears the
        # cache, A resumes and writes the stale entry back -- so
        # sd_get_file_size answers 3 for a device we have stopped talking to,
        # where its documented miss value is 0.
        #
        # ⭐ `disconnect()` does not invalidate because the data is old. It
        # invalidates because THE DEVICE'S STATE IS NO LONGER KNOWN, and
        # nothing a listing finished afterwards can re-establish that. So the
        # invalidation is authoritative and a listing that completes across one
        # is DISCARDED, not published.
        #
        # The generation counter is what makes that decidable: captured before
        # the query, compared at the publish. Equal means no invalidation
        # intervened and this listing still describes a live cache; changed
        # means one did, and the right cache is the empty one the invalidation
        # left. Still exactly one write to the attribute, so the
        # half-populated window r10 closed stays closed.
        sizes = {}
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
                    sizes[path] = size
                else:
                    # Old format: just path, no size. Stored as None, not
                    # 0: "this file is empty" and "this listing did not
                    # say" are different facts, and sd_get_file_size()
                    # reports the second as 0 without claiming the first.
                    files.append(line)
                    sizes[line] = None
        # The single publish, and only if no invalidation intervened. ⛔ r11
        # :835: the comparison and the assignment are ONE step under the lock.
        # Separately they are a check-then-act, and an invalidation arriving
        # between them published stale sizes -- the same class as :830, which
        # this publish was introduced to fix.
        with self._file_sizes_lock:
            if self._file_sizes_generation == generation_at_entry:
                self._file_sizes = sizes
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
        """The last listing's size for ``filename``, or ``None`` if that
        listing did not state one or did not contain the file.

        ⚠ READ ONLY BY :meth:`sd_get_file_size`. The download used to pass
        this to the receiver as an enforced boundary; that is gone with the
        legacy path's size logic (ruling FO A2), so a wrong answer here is
        now a wrong number rather than a rejected transfer.

        ⛔ EXACT KEY ONLY, and two softer rules were tried and removed.
        A listing stores what the device PRINTED -- ``DAQiFi/log.bin`` --
        while callers hold bare names, so matching on the final path
        segment looked like the fix. It BORROWS ACROSS DIRECTORIES: with
        ``OTHER/log.bin`` listed at 3 bytes, ``log.bin`` resolved to 3.
        Joining the name to a listed directory fails the same way, because
        the device resolves a bare GET in its OWN default directory, not in
        whichever one was listed last. Neither is done.

        A listed 0 is an empty file and is returned as 0; "no size given"
        is ``None``, which is what makes the ``is not None`` test below
        meaningful rather than a truthiness accident.
        """
        # ⛔ r9 :830 (MEDIUM) -- ONE DICT OPERATION, NEVER CHECK-THEN-INDEX.
        # This read `if filename not in self._file_sizes` and then indexed
        # `self._file_sizes[filename]`, two separate reads of the attribute.
        # `_forget_sd_file_sizes` REPLACES it (`self._file_sizes = {}`,
        # below) and is wired to StreamManager's recording-change callback,
        # which runs ON ANOTHER THREAD -- AsyncStreamManager.stop() in
        # particular. So a listing could satisfy the membership check and the
        # index could then hit the fresh empty dict and raise KeyError at the
        # caller. The base had a single `dict.get()` here, so the window was
        # INTRODUCED by splitting the lookup in two.
        #
        # `.get()` is one operation on one object: whichever dict is current
        # when it runs answers, and a replacement racing it means the caller
        # sees the old cache or the new empty one -- both legitimate answers,
        # neither an exception. Taking a local reference first would ALSO fix
        # the crash, but `.get()` needs no reasoning about how the clear is
        # spelled, so it survives someone changing it to `.clear()`.
        #
        # ⚠ `.get()` with no default returns None for a MISSING key, which is
        # this method's documented miss value, and 0 for a file the listing
        # said is 0 bytes. That distinction is the point of the old
        # `is not None` dance and it is preserved exactly: a listed 0 comes
        # back as 0, never as None and never falsy-collapsed. Do NOT
        # reintroduce an `if size:` here -- it sent an empty file down the
        # unsized path, and in an earlier revision that also matched
        # basenames it let that file borrow another directory's size.
        return self._file_sizes.get(filename)

    def _forget_sd_file_sizes(self) -> None:
        """Drop the cached listing sizes.

        ⛔ CALLED FROM THE EIGHT OPERATIONS FOUND TO CHANGE THE CARD, so
        :meth:`sd_get_file_size` does not report a length from before a
        recording. ⚠ "Eight" is the enumeration I ran -- by what WRITES,
        not by what is named ``sd_*``, which is how StreamManager was missed
        the first time -- and not a proof of completeness. I claimed that
        twice and was wrong twice. A ninth path would now cost a wrong
        number from one getter, which is why the list no longer has to be
        perfect. ⚠ The download no longer reads these numbers at all
        (ruling FO A2), so a stale entry is now a wrong answer from that
        getter rather than a rejected transfer -- it was the latter, which
        is what made staleness worth eight invalidation points.

        Forgetting everything rather than tracking what changed is still the
        right trade: the cost is one extra listing, and the next listing
        restores it.

        Mirrors ``DaqifiStreamingDevice._forget_sd_card_sizes``, which does
        the same for the modern path -- including on disconnect, since a
        listing from an earlier session is not a safe length either.
        """
        with self._file_sizes_lock:
            self._file_sizes = {}
            self._file_sizes_generation += 1

    def sd_get_file(self, filename: str, timeout: float = 30.0) -> str:
        """
        Retrieve file contents from SD card.

        Args:
            filename: Name of file to retrieve
            timeout: Maximum time to wait for file transfer (default: 30s).
                Must be FINITE: a non-finite value raises ValueError, because
                the read has no other bound (ruling HW A). One rare shape
                waits out this whole timeout and then returns the correct
                bytes -- a file whose content is exactly the echoed command's
                prefix AND whose name begins with ``__END_OF_FILE__``. See
                ``_read_sd_response`` and python-core #260.

        Returns:
            File contents as string

        Example:
            data = device.sd_get_file("test.csv")
            print(data)

        Raises:
            TimeoutError: no ``__END_OF_FILE__`` arrived within ``timeout``.
            RuntimeError: the serial port is not open.

        ⚠ Does NOT raise ``SdCardTransferErrorException``. A file may
        legitimately contain ``__TRANSFER_ERROR__``, so this path stopped
        classifying it (ruling FO A2): a failed transfer reads as a short
        file or a timeout, as it did before this PR. #253 is where the
        reason becomes available, by asking the error queue.
        """
        # ⛔ RULING HW A -- VALIDATED BEFORE THE PORT CHECK AND BEFORE THE
        # WRITE. A usage error must not depend on port state, and refusing
        # AFTER the GET went out would leave the device streaming a file
        # nobody will read.
        timeout = _require_finite_sd_timeout(timeout, "sd_get_file")
        if not self._comm.is_open():
            raise RuntimeError("Serial port not open")

        # Send GET command
        cmd = scpi_commands.get_sd_file(filename).data
        cmd_bytes = (cmd + "\r").encode('ascii')
        self._comm._serial.write(cmd_bytes)
        # #263: bounded, no flush(); a stall raises SerialTimeoutException.
        drain_output_bounded(
            self._comm._serial,
            getattr(self._comm, "send_drain_timeout", SEND_DRAIN_TIMEOUT_S))

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
            timeout: Maximum time to wait for file transfer (default: 30s).
                Must be FINITE -- see the sibling ``sd_get_file`` and ruling
                HW A. The same rare echo-prefix shape waits out the timeout
                and then returns the correct bytes; python-core #260 is the
                out-of-band fix.

        Returns:
            File contents as raw bytes

        Raises:
            TimeoutError: If __END_OF_FILE__ marker not received within timeout
            RuntimeError: If serial port not open

        ⚠ Does NOT raise ``SdCardTransferErrorException`` -- see
        :meth:`sd_get_file`.

        Example:
            data = device.sd_get_file_binary("benchmark.dat")
            assert len(data) == expected_size
        """
        # ⛔ RULING HW A -- see the sibling. Both entries guard, because both
        # reach the same unbounded head-resolution wait and a caller reaching
        # only one of them would still hang.
        timeout = _require_finite_sd_timeout(timeout, "sd_get_file_binary")
        if not self._comm.is_open():
            raise RuntimeError("Serial port not open")

        # Send GET command
        cmd = scpi_commands.get_sd_file(filename).data
        cmd_bytes = (cmd + "\r").encode('ascii')
        self._comm._serial.write(cmd_bytes)
        # #263: bounded, no flush(); a stall raises SerialTimeoutException.
        drain_output_bounded(
            self._comm._serial,
            getattr(self._comm, "send_drain_timeout", SEND_DRAIN_TIMEOUT_S))

        return self._receive_sd_file(filename, cmd, timeout, idle_reset=False)

    def _read_sd_response(
        self, timeout: float, idle_reset: bool, echo: bytes = b"",
    ) -> bytes:
        """Read the GET response off the port. Transport only -- what the
        bytes MEAN is the receiver's job.

        Stops at the first ``__END_OF_FILE__`` that is not inside the
        echoed command, which is the base's rule. ⛔ NEVER SCAN THE ECHO: a
        GET names a file, and a file can be named ``__END_OF_FILE__``, so
        the echoed command can carry the marker's own bytes and stopping on
        that ends the capture before the payload. ``head_span`` is what THIS
        loop uses to decide where the head ends; ``None`` means it is still
        arriving.

        ⚠⚠ r14 -- AN EARLIER REVISION OF THIS LINE SAID ``head_span`` was
        "shared with the receiver that strips it". It is not: the receiver
        strips through ``_PreambleSkip.feed``/``flush``, which run their own
        prefix match, so there are TWO implementations of where the head ends.
        They agree by construction and are now pinned equal by
        ``test_head_span_and_the_strip_agree_on_every_shape``; before that pin
        nothing enforced it, and a mutation aimed at one of them came back
        green because the other still worked.

        ⛔ NO SIZE, AND NO GRACE PERIOD. Both are gone with the legacy
        path's size logic (ruling FO A2). The size-aware stop rule and the
        quiet-line grace between them produced every HIGH from r3 and r4:
        framing counted toward a payload target, and marker-shaped content
        ended the capture early. What they bought -- reading past a marker
        inside file content -- is a capability neither #154 nor #206 asked
        for and the base never had.

        ``idle_reset`` keeps the text path's original semantics, where the
        timeout measures SILENCE rather than total duration, so a slow but
        progressing download is not cut off. The binary path's timeout was
        absolute and stays that way.

        ⚠⚠ ONE ACCEPTED LATENCY CASE, under ruling HW A. The marker scan is
        gated on the head being RESOLVED, and ``head_span`` answers None while
        the buffer is a proper prefix of a candidate. A payload that is itself
        such a prefix therefore keeps the head unresolved, and the marker it
        has ALREADY received is never looked for: the call waits out the
        caller's timeout and THEN returns the correct bytes.

        Reaching it needs two coincidences -- the content must be exactly the
        echo's prefix AND the filename must begin with ``__END_OF_FILE__``, so
        that the marker's bytes CONTINUE the echo instead of breaking the
        prefix. An ordinary filename does not stall.

        ⛔ IT IS NOT FIXABLE FROM THIS BUFFER. A complete payload of that
        shape and a PARTIAL ECHO for that same file are BYTE-IDENTICAL here,
        and the first must stop while the second must keep waiting. Ending the
        capture on a marker with the head unresolved was measured: it returns
        the 23-byte echo fragment in place of a real file, which is the silent
        short-file direction ruling GV A rejected. The real discriminator is
        the device's ECHO STATE, which this module never reads -- python-core
        #260. Both directions are pinned in
        ``tests/test_sd_timeout_bound_and_echo_prefix_257.py``.
        """
        from .sdcard import _PreambleSkip

        serial = self._comm._serial
        out = bytearray()
        deadline = time.monotonic() + timeout
        skip = _PreambleSkip(echo)
        # ⛔ r10 :1028 (MEDIUM) -- SCAN INCREMENTALLY, AND NEVER COPY THE
        # BUFFER. This loop did `buf = bytes(out)` and then
        # `END_OF_FILE_MARKER in buf[head:]` on EVERY iteration, idle polls
        # included: a full copy of everything received so far, plus a full
        # rescan of it, per 4 KiB read. That is O(n^2) over a download and it
        # turned a working transfer into a TimeoutError. Measured by the audit
        # on a 32 MiB source fed in 4096-byte chunks with no delays: the base
        # returned all 33,554,432 bytes in 22.8 s, while this code raised
        # TimeoutError at 30 s having read only ~21 MB.
        #
        # `head` is resolved ONCE. `head_span` answers None only while the
        # preamble may still be arriving; once it returns a span that span is
        # final, so re-asking it per iteration bought nothing.
        #
        # `scanned` is how far the marker search has already looked.
        # Re-searching from `scanned - (len(marker) - 1)` is what makes the
        # incremental scan exact rather than merely fast: a marker split
        # across two reads straddles that boundary, and without the overlap it
        # would be missed by both passes. `bytearray.find` searches in place,
        # so no copy is made.
        head = None
        scanned = 0
        overlap = len(END_OF_FILE_MARKER) - 1
        while time.monotonic() < deadline:
            pending = serial.in_waiting
            if pending:
                out.extend(serial.read(pending))
            if head is None:
                head = skip.head_span(out)
            done = False
            if head is not None:
                start = scanned - overlap
                if start < head:
                    start = head
                done = out.find(END_OF_FILE_MARKER, start) != -1
                scanned = len(out)
            # ⛔ r10 :1027 (MEDIUM, blind leg) -- THE IDLE DEADLINE IS RESET
            # AFTER THE WORK, NOT BEFORE IT. The reset used to sit inside the
            # `if pending` block above, so the time spent copying and scanning
            # was charged against the NEXT silence window rather than excluded
            # from it. With a short timeout and a continuously-fed source the
            # loop then exited mid-transfer: the audit measured
            # sd_get_file('log.csv', timeout=0.001) against 8 MiB supplied in
            # 4096-byte reads ending with ~3 MiB captured, over 5 MiB still
            # unread, and a TimeoutError on reparse. The base reset after
            # scanning, so this restores that order rather than inventing one.
            #
            # ⭐ This is what makes the parameter mean what it says: the
            # docstring promises the timeout measures SILENCE, and silence
            # cannot include the host's own processing time. Fixing :1028
            # shrinks that processing time, but the ORDER is the correctness
            # fix and would be needed even if the scan were free.
            if pending and idle_reset:
                deadline = time.monotonic() + timeout
            if done:
                break
            if not pending:
                time.sleep(0.01)
        return bytes(out)


    def _receive_sd_file(
        self, filename: str, cmd: str, timeout: float, idle_reset: bool,
    ) -> bytes:
        """The SD receive contract, applied by the one implementation of it.

        ⛔ THIS USED TO BE A SECOND COPY OF THAT CONTRACT (#154, #206 item
        5): it hardcoded ``__END_OF_FILE__``, ended the read at the first
        occurrence ANYWHERE so a file containing those bytes truncated
        itself, found the prompt by searching 200 bytes so a file containing
        ``DAQIFI>`` lost its head, and swept the command, prompt and marker
        out with ``.replace()`` at every offset. Those are what the
        delegation fixes.

        ⛔ WHAT IT DELIBERATELY DOES NOT DO, by ruling FO A2:

        - **no listed size.** An unsized transfer ends at the first terminal
          marker, so a file whose content contains ``__END_OF_FILE__`` is
          truncated there. The size machinery that read past it produced
          both of r3's HIGHs, r4's HIGH and three of r2's five defects, and
          delivered a capability neither ticket requested.

          ⛔ AND THAT IS A BEHAVIOUR CHANGE ON THE TEXT PATH, not "the
          base's behaviour" as r5's revision of this docstring claimed. The
          claim was false and is withdrawn rather than softened. Measured
          on ``b'head__END_OF_FILE__tail__END_OF_FILE__'``: the base
          returned ``'headtail'`` -- its ``.replace()`` deleted the marker's
          bytes at EVERY offset and kept everything around them -- where
          this returns ``'head'``. So for a text file whose content contains
          the marker, the base corrupted the payload in place and this
          truncates it instead.

          Both are lossy and neither is recoverable without a size, which
          is the point: with no length there is nothing that distinguishes
          a marker ending the file from the same bytes inside it. Ruling
          FO A2 chose truncation, and reading past the marker is the
          mechanism that ruling removed -- so it is NOT re-added here to
          close r5's finding. What r5 actually disproved was the claim, and
          the claim is what changes.
        - **no error-marker detection** (``eof_only=True``). A file may
          legitimately contain ``__TRANSFER_ERROR__``, and the overlap at a
          join -- content ending ``...__TRANSFER_ERROR`` followed by the
          real ``__END_OF_FILE__`` -- completes those bytes by accident. On
          this path a false device-error is worse than the base's silence,
          and fixing it in the shared scan would have reached the two
          already-correct callers. So the legacy path stops classifying
          error markers and ``_find_terminal_marker`` is untouched.

        ⚠ Consequence, stated because callers depend on it: this method no
        longer raises ``SdCardTransferErrorException``. A failed transfer
        reads as a short file or a timeout, exactly as before this PR. #253
        is where the reason becomes available, by asking the error queue.
        """
        import io

        from .sdcard import SdCardFileReceiver

        raw = self._read_sd_response(
            timeout, idle_reset, echo=cmd.encode("ascii"),
        )
        destination = io.BytesIO()
        SdCardFileReceiver(io.BytesIO(raw)).receive(
            destination, filename,
            # ⛔ NOT ``timeout``. The transfer is already over: ``raw`` holds
            # every byte, and this pass re-reads an in-memory buffer that
            # cannot block. Passing the caller's timeout made it a SECOND
            # absolute deadline over local parsing, so a transfer that had
            # completely succeeded raised TimeoutError whenever the parse
            # took longer than the transport allowance -- measured in r5 with
            # ``timeout=0.001`` and 8 MiB already in hand, where the base
            # returned all 8,388,608 characters and this raised.
            #
            # A timeout is still required by the signature, so this is a
            # LIVENESS BOUND on a pure-CPU pass, not a transport timeout: it
            # exists only so a defect in the parser cannot hang forever, and
            # it must never be derived from the caller's value.
            timeout_s=_LOCAL_REPARSE_LIVENESS_S,
            # The command as sent: the console may echo it back ahead of the
            # payload, and it is removed as a PREFIX, never searched for.
            preamble=cmd.encode("ascii"),
            eof_only=True,
        )
        return destination.getvalue()


    def sd_delete_file(self, filename: str):
        """
        Delete a file from SD card.

        Args:
            filename: Name of file to delete

        Example:
            device.sd_delete_file("old_data.csv")
        """
        # Can change what is on the card, so a listed size is no longer a
        # safe answer from sd_get_file_size() (see _forget_sd_file_sizes).
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
        # Can change what is on the card, so a listed size is no longer a
        # safe answer from sd_get_file_size() (see _forget_sd_file_sizes).
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
        # Can change what is on the card, so a listed size is no longer a
        # safe answer from sd_get_file_size() (see _forget_sd_file_sizes).
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
