# portomatic apply prompt

**Plan:** `20260419T233723-port-imessageproducer-v05`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free

## Step

- summary: Port IMessageProducer<T> interface (4 methods + 2 properties + IDisposable) to a Python Protocol or ABC
- classification: new-feature
- shard: Communication
- upstream_ref: (unset)
- downstream_target: daqifi/_internal/comm.py

## Upstream file at this commit (full)

```csharp
using Daqifi.Core.Communication.Messages;

namespace Daqifi.Core.Communication.Producers;

/// <summary>
/// Interface for message producers that handle queuing and sending messages to devices.
/// </summary>
/// <typeparam name="T">The type of message data to produce.</typeparam>
public interface IMessageProducer<T> : IDisposable
{
    /// <summary>
    /// Starts the message producer, beginning background message processing.
    /// </summary>
    void Start();

    /// <summary>
    /// Stops the message producer immediately, clearing any pending messages.
    /// </summary>
    void Stop();

    /// <summary>
    /// Stops the message producer safely, waiting for pending messages to be sent.
    /// </summary>
    /// <param name="timeoutMs">Maximum time to wait for pending messages in milliseconds.</param>
    /// <returns>True if all messages were sent, false if timeout occurred.</returns>
    bool StopSafely(int timeoutMs = 1000);

    /// <summary>
    /// Queues a message for sending to the device.
    /// </summary>
    /// <param name="message">The message to send.</param>
    void Send(IOutboundMessage<T> message);

    /// <summary>
    /// Gets the number of messages currently queued for sending.
    /// </summary>
    int QueuedMessageCount { get; }

    /// <summary>
    /// Gets a value indicating whether the producer is currently running.
    /// </summary>
    bool IsRunning { get; }
}
```

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

-       line 55: `# Wait for device to stabilize`
            `time.sleep(0.1)`
-       line 57: `# Clear any pending data`
            `self._serial.reset_input_buffer()`
            `self._serial.reset_output_buffer()`
            `return True`
            `except (serial.SerialException, OSError) as e:`
            `return False`
-       line 92: `# Send command with CR terminator`
            `cmd_bytes = (command + "\r").encode('ascii')`
            `self._serial.write(cmd_bytes)`
            `self._serial.flush()`
-       line 97: `# Wait for device to process`
            `time.sleep(delay)`
-       line 100: `# Read all available data`
            `response = b""`
            `start_time = time.time()`
            `while (time.time() - start_time) < self.timeout:`
            `if self._serial.in_waiting > 0:`
            `chunk = self._serial.read(self._serial.in_waiting)`
-           line 108: `# Give a bit more time for any trailing data`
                `time.sleep(0.05)`
                `if self._serial.in_waiting == 0:`
                `break`
                `else:`
-           line 114: `# No data yet, wait a bit`
                `time.sleep(0.01)`
-       line 117: `# Decode and clean response`
            `response_str = response.decode('ascii', errors='ignore')`
-       line 120: `# Remove echoed command if present`
            `if command in response_str:`
            `response_str = response_str.replace(command, "")`
            `response_str = response_str.replace("DAQIFI>", "")`
-       line 127: `# Clean up whitespace and control characters`
            `response_str = response_str.strip()`
            `return response_str`
            `except (serial.SerialException, OSError) as e:`
            `raise RuntimeError(f"Serial communication error: {e}")`
            `def send_query(self, query: str, delay: float = 0.1) -> str:`
-     line 147: `# Remove any surrounding quotes from response`
          `response = response.strip('"').strip("'")`
          `return response`
          `def send_set(self, command: str, value, delay: float = 0.1) -> bool:`
-     line 163: `# Format value based on type`
          `if isinstance(value, str):`
          `cmd = f"{command} {value}"`
          `elif isinstance(value, bool):`
          `cmd = f"{command} {1 if value else 0}"`
          `elif isinstance(value, (int, float)):`
-     line 175: `# Check for errors`
          `error = self.send_query("SYST:ERR?", 0.1)`
          `return "No error" in error`
          `def clear_errors(self):`
          `max_errors = 10`
          `for _ in range(max_errors):`

```python
"""
Internal serial communication layer for DAQiFi Nyquist devices.

This module provides low-level SCPI command interface over serial port.
Not intended for direct use - use the public API instead.
"""

import serial
import time
from typing import Optional
from enum import IntEnum
import builtins


class PowerState(IntEnum):
    """Device power states (matches firmware PowerApi.h)"""
    STANDBY = 0            # MCU on if USB powered, off if battery powered
    POWERED_UP = 1         # Board fully powered (all modules active)
    POWERED_UP_EXT_DOWN = 2  # Partial power, external power disabled (low battery mode)


class SerialInterface:
    """Low-level serial communication with Nyquist devices"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        """
        Initialize serial connection parameters.

        Args:
            port: Serial port path (e.g., /dev/ttyACM0 on Linux, COM3 on Windows)
            baudrate: Serial baud rate (default 115200)
            timeout: Read timeout in seconds
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None

    def open(self) -> bool:
        """
        Open serial connection.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            # Wait for device to stabilize
            time.sleep(0.1)
            # Clear any pending data
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            return True
        except (serial.SerialException, OSError) as e:
            # Return False instead of raising to allow discovery to continue
            return False

    def close(self):
        """Close serial connection"""
        if self._serial and self._serial.is_open:
            self._serial.close()

    def is_open(self) -> bool:
        """Check if serial connection is open"""
        return self._serial is not None and self._serial.is_open

    def send_command(self, command: str, delay: float = 0.1) -> str:
        """
        Send SCPI command and read response.

        Args:
            command: SCPI command string (without terminator)
            delay: Delay in seconds before reading response

        Returns:
            Response string (stripped of whitespace)

        Raises:
            RuntimeError: If serial port not open or communication fails
        """
        if not self.is_open():
            raise RuntimeError("Serial port not open")

        try:
            # Send command with CR terminator
            cmd_bytes = (command + "\r").encode('ascii')
            self._serial.write(cmd_bytes)
            self._serial.flush()

            # Wait for device to process
            time.sleep(delay)

            # Read all available data
            response = b""
            start_time = time.time()

            while (time.time() - start_time) < self.timeout:
                if self._serial.in_waiting > 0:
                    chunk = self._serial.read(self._serial.in_waiting)
                    response += chunk
                    # Give a bit more time for any trailing data
                    time.sleep(0.05)
                    # If nothing more comes, we're done
                    if self._serial.in_waiting == 0:
                        break
                else:
                    # No data yet, wait a bit
                    time.sleep(0.01)

            # Decode and clean response
            response_str = response.decode('ascii', errors='ignore')

            # Remove echoed command if present
            if command in response_str:
                response_str = response_str.replace(command, "")

            # Remove DAQIFI> prompt if present
            response_str = response_str.replace("DAQIFI>", "")

            # Clean up whitespace and control characters
            response_str = response_str.strip()

            return response_str

        except (serial.SerialException, OSError) as e:
            raise RuntimeError(f"Serial communication error: {e}")

    def send_query(self, query: str, delay: float = 0.1) -> str:
        """
        Send SCPI query (command ending with '?') and return response.

        Args:
            query: SCPI query string (should end with '?')
            delay: Delay in seconds before reading response

        Returns:
            Response value (stripped of whitespace and quotes)
        """
        response = self.send_command(query, delay)
        # Remove any surrounding quotes from response
        response = response.strip('"').strip("'")
        return response

    def send_set(self, command: str, value, delay: float = 0.1) -> bool:
        """
        Send SCPI set command with value.

        Args:
            command: SCPI command string (without value)
            value: Value to set
            delay: Delay in seconds before reading response

        Returns:
            True if command succeeded (no error), False otherwise
        """
        # Format value based on type
        if isinstance(value, str):
            cmd = f"{command} {value}"
        elif isinstance(value, bool):
            cmd = f"{command} {1 if value else 0}"
        elif isinstance(value, (int, float)):
            cmd = f"{command} {value}"
        else:
            cmd = f"{command} {str(value)}"

        self.send_command(cmd, delay)

        # Check for errors
        error = self.send_query("SYST:ERR?", 0.1)
        return "No error" in error

    def clear_errors(self):
        """Clear error queue by reading all errors"""
        max_errors = 10
        for _ in range(max_errors):
            error = self.send_query("SYST:ERR?", 0.05)
            if "No error" in error:
                break

    def reset(self):
        """Send *RST command to reset device"""
        self.send_command("*RST", delay=0.5)
        self.clear_errors()

    def __enter__(self):
        """Context manager entry"""
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()

```

## Map shard `Communication` — relevant entries


### Architectural context (no exact file match)

_Nearest file mappings in this shard — read these BEFORE deciding whether to create a downstream file:_

- `src/Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs` → `daqifi/_internal/scpi.py` (divergent)

## Output requirements

- Edit the downstream file (or create it if missing) to implement the upstream change in idiomatic Python.
- Do NOT transliterate — port. Use Python idioms (dataclasses, properties, type hints, exceptions).
- Honour any active divergences listed above (don't 'fix' an intentional divergence).
- **Match the LOCAL section's convention**, not the whole file's most-common style. If the File regions index above shows multiple sections, identify which section your change belongs to and mimic THAT section's naming/formatting — different sections often use different conventions intentionally.
- For Python smoke checks, use `python3` (auto-detected for this project — venv preferred, else python3).
- After editing, run the verifier policy. Bound at N=3 reflection iterations.
- **On failure (verifier still failing after 3 attempts), STOP and report.** Do NOT call `AskUserQuestion`, do NOT file a sub-ticket yourself, do NOT keep iterating past N=3. Just include in your report:
  - The diff you applied (latest attempt)
  - The verifier output (last ~20 lines)
  - Your best guess at the failure class: `compile`, `behavioral_drift`, `missing_dep`, `flaky_test`, or `unknown`
  - The orchestrator marks the ticket `blocked`, surfaces the failure to the user, and decides skip/retry/abort.
- **DO NOT run `portomatic apply close` yourself** — the orchestrator that dispatched you owns the close step. Just report the diff you applied, whether tests passed, and any blockers. The orchestrator runs `apply close` after verifying.
