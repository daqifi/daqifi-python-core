"""
Main device controller for DAQiFi Nyquist devices.

Provides high-level API for device interaction.
"""

from typing import Dict, Any, Optional, Union, List
from ._internal.comm import SerialInterface, PowerState
from ._internal import scpi
from .channels import (
    AnalogOutputChannel,
    AnalogInputChannel,
    DigitalIOChannel,
    AnalogOutputs,
    AnalogInputs,
    DigitalIO
)
from .streaming import StreamManager, AsyncStreamManager
from .discovery import DeviceInfo, BoardVariant
from .exceptions import (
    ConnectionError as DAQiFiConnectionError,
    CommandError,
    FeatureNotSupportedError
)


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
            idn = self._comm.send_query(scpi.System.IDN)
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
            adc_channels = [AnalogInputChannel(i, self._comm, self._variant.value) for i in range(num_adc)]
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
        """Get device serial number"""
        return self._device_info.get("serial_number", "Unknown")

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
            # Set voltage
            device.analog_outputs[0].voltage = 5.0

            # Enable channel
            device.analog_outputs[0].enabled = True
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
            response = self._comm.send_query(scpi.System.POWER_STATE_QUERY)
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
        try:
            success = self._comm.send_set(scpi.System.POWER_STATE_SET, int(state), delay=1.0)
            if not success:
                error = self._comm.send_query(scpi.System.ERR)
                raise CommandError(f"Failed to set power state: {error}")
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
            return self._comm.send_query(scpi.System.INFO)
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
            return self._comm.send_query(scpi.System.ERR)
        except Exception as e:
            raise CommandError(f"Failed to query error: {e}")

    def clear_errors(self):
        """
        Clear device error queue.

        Example:
            device.clear_errors()
        """
        self._comm.clear_errors()

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
        from ._internal.scpi import System
        self._comm.send_command(f"{System.SD_ENABLE_SET} {1 if enable else 0}")

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
        from ._internal.scpi import System
        if directory:
            response = self._comm.send_command(f'{System.SD_LIST_QUERY} "{directory}"')
        else:
            response = self._comm.send_command(System.SD_LIST_QUERY)

        # Parse file list from response
        files = []
        for line in response.strip().split('\n'):
            line = line.strip()
            # Skip empty lines and prompts
            if line and not line.startswith('DAQIFI') and not line.startswith('#'):
                files.append(line)
        return files

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
        from ._internal.scpi import System

        if not self._comm.is_open():
            raise RuntimeError("Serial port not open")

        # Send GET command
        cmd = f'{System.SD_GET} "{filename}"'
        cmd_bytes = (cmd + "\r").encode('ascii')
        self._comm._serial.write(cmd_bytes)
        self._comm._serial.flush()

        # Read until we see __END_OF_FILE__ marker
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

    def sd_delete_file(self, filename: str):
        """
        Delete a file from SD card.

        Args:
            filename: Name of file to delete

        Example:
            device.sd_delete_file("old_data.csv")
        """
        from ._internal.scpi import System
        self._comm.send_command(f'{System.SD_DELETE} "{filename}"')

    def sd_format(self):
        """
        Format SD card (erase all files).

        WARNING: This will erase ALL files on the SD card!

        Example:
            device.sd_format()  # Careful!
        """
        from ._internal.scpi import System
        self._comm.send_command(System.SD_FORMAT)

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
        from ._internal.scpi import System
        self._comm.send_command(f'{System.SD_LOGGING_SET} "{filename}"')

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
        from ._internal.scpi import System
        # Run benchmark
        self._comm.send_command(f"{System.SD_BENCHMARK} {size_kb}")

        # Query results
        response = self._comm.send_command(System.SD_BENCHMARK_QUERY)
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

        # Create appropriate stream manager
        if async_mode:
            return AsyncStreamManager(self._comm, channels, sample_rate, format)
        else:
            return StreamManager(self._comm, channels, sample_rate, format)

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
