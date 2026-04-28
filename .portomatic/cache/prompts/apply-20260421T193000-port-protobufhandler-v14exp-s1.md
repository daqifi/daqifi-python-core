# portomatic apply prompt

**Plan:** `20260421T193000-port-protobufhandler-v14exp`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: Port ProtobufProtocolHandler: 4-handler dispatch + DetectMessageType static. Used as the prompt-driven side of the #118 head-to-head experiment.
- classification: new-feature
- shard: Communication
- upstream_ref: e8d84af:src/Daqifi.Core/Device/Protocol/ProtobufProtocolHandler.cs
- downstream_target: daqifi/protocol.py

## Port conventions (project-wide)

- `Task<T> (async)` → `Awaitable[T]` if the project uses asyncio; sync return otherwise (note divergence in shard)
- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Stream` → `typing.IO[bytes]` (or `BinaryIO`) — file-like objects with read/write
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.
- `Func<T1,...,Tn,R> / Action<T> / Func<R>` → `Callable[[T1, ..., Tn], R]` (from `typing`). `Action<T>` → `Callable[[T], None]`. `Func<R>` (no params) → `Callable[[], R]`. For optional callbacks use `Callable[..., R] | None = None`.

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

In addition: this project has a pytest surface at `tests/portomatic-generated/`. Write unit tests for the ported symbol at `tests/portomatic-generated/test_<slug>.py`; cover all branches and edge cases; run `python3 -m pytest tests/portomatic-generated/test_<slug>.py -q` and include the summary line in your report.

## Target symbol: `ProtobufProtocolHandler`

This step ports ONLY `ProtobufProtocolHandler` from the upstream file.

## Upstream file at this commit (full)

```csharp
using Daqifi.Core.Communication.Messages;
using System;
using System.Threading.Tasks;

namespace Daqifi.Core.Device.Protocol;

/// <summary>
/// Handles protobuf-based protocol messages from DAQiFi devices.
/// </summary>
public class ProtobufProtocolHandler : IProtocolHandler
{
    private readonly Action<DaqifiOutMessage>? _statusMessageHandler;
    private readonly Action<DaqifiOutMessage>? _streamMessageHandler;
    private readonly Action<DaqifiOutMessage>? _sdCardMessageHandler;
    private readonly Action<DaqifiOutMessage>? _errorMessageHandler;

    /// <summary>
    /// Initializes a new instance of the <see cref="ProtobufProtocolHandler"/> class.
    /// </summary>
    /// <param name="statusMessageHandler">Optional handler for status messages.</param>
    /// <param name="streamMessageHandler">Optional handler for streaming messages.</param>
    /// <param name="sdCardMessageHandler">Optional handler for SD card messages.</param>
    /// <param name="errorMessageHandler">Optional handler for error messages.</param>
    public ProtobufProtocolHandler(
        Action<DaqifiOutMessage>? statusMessageHandler = null,
        Action<DaqifiOutMessage>? streamMessageHandler = null,
        Action<DaqifiOutMessage>? sdCardMessageHandler = null,
        Action<DaqifiOutMessage>? errorMessageHandler = null)
    {
        _statusMessageHandler = statusMessageHandler;
        _streamMessageHandler = streamMessageHandler;
        _sdCardMessageHandler = sdCardMessageHandler;
        _errorMessageHandler = errorMessageHandler;
    }

    /// <summary>
    /// Determines whether this handler can process the specified message.
    /// </summary>
    /// <param name="message">The message to evaluate.</param>
    /// <returns><c>true</c> if the message is a DaqifiOutMessage; otherwise, <c>false</c>.</returns>
    public bool CanHandle(IInboundMessage<object> message)
    {
        return message.Data is DaqifiOutMessage;
    }

    /// <summary>
    /// Processes the specified protobuf message and routes it to the appropriate handler.
    /// </summary>
    /// <param name="message">The message to process.</param>
    /// <returns>A task representing the asynchronous operation.</returns>
    public Task HandleAsync(IInboundMessage<object> message)
    {
        if (message.Data is not DaqifiOutMessage pbMessage)
        {
            return Task.CompletedTask;
        }

        var messageType = DetectMessageType(pbMessage);

        switch (messageType)
        {
            case ProtobufMessageType.Status:
                _statusMessageHandler?.Invoke(pbMessage);
                break;

            case ProtobufMessageType.Stream:
                _streamMessageHandler?.Invoke(pbMessage);
                break;

            case ProtobufMessageType.SdCard:
                _sdCardMessageHandler?.Invoke(pbMessage);
                break;

            case ProtobufMessageType.Error:
                _errorMessageHandler?.Invoke(pbMessage);
                break;

            case ProtobufMessageType.Unknown:
            default:
                // Unknown message type - could log here if needed
                break;
        }

        return Task.CompletedTask;
    }

    /// <summary>
    /// Detects the type of a protobuf message based on its contents.
    /// </summary>
    /// <param name="message">The protobuf message to analyze.</param>
    /// <returns>The detected message type.</returns>
    public static ProtobufMessageType DetectMessageType(DaqifiOutMessage message)
    {
        // Status messages contain device configuration information
        if (IsStatusMessage(message))
        {
            return ProtobufMessageType.Status;
        }

        // Streaming messages contain analog/digital data with timestamps
        if (IsStreamMessage(message))
        {
            return ProtobufMessageType.Stream;
        }

        // SD card messages are typically text-based and handled separately
        // (This is a placeholder - actual SD card messages come as text responses, not protobuf)

        // Error messages contain error status
        if (message.DeviceStatus != 0)
        {
            return ProtobufMessageType.Error;
        }

        return ProtobufMessageType.Unknown;
    }

    /// <summary>
    /// Determines if a message is a status message containing device configuration.
    /// </summary>
    /// <param name="message">The message to check.</param>
    /// <returns><c>true</c> if the message is a status message; otherwise, <c>false</c>.</returns>
    private static bool IsStatusMessage(DaqifiOutMessage message)
    {
        // Status messages contain channel configuration information
        return message.DigitalPortNum != 0 ||
               message.AnalogInPortNum != 0 ||
               message.AnalogOutPortNum != 0;
    }

    /// <summary>
    /// Determines if a message is a streaming data message.
    /// </summary>
    /// <param name="message">The message to check.</param>
    /// <returns><c>true</c> if the message contains streaming data; otherwise, <c>false</c>.</returns>
    private static bool IsStreamMessage(DaqifiOutMessage message)
    {
        // Stream messages contain timestamp and data.
        // USB firmware sends AnalogInDataFloat (pre-scaled floats) while WiFi firmware
        // may send AnalogInData (raw integer ADC counts). Both must be detected.
        return message.MsgTimeStamp != 0 &&
               (message.AnalogInData.Count > 0 ||
                message.AnalogInDataFloat.Count > 0 ||
                message.DigitalData.Length > 0);
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

- `IProtocolHandler` **[base contract]** **[already ported in this target file at line 16]** — see the file body above.
- `DaqifiOutMessage` (from `src/Daqifi.Core/Communication/Messages/DaqifiOutMessage.cs`) _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Communication/Messages/DaqifiOutMessage.cs` for the full contract)_
### `ProtobufMessageType` (from `src/Daqifi.Core/Device/Protocol/ProtobufMessageType.cs`) **[already ported as `daqifi/enums.py:52`]**

```csharp
public enum ProtobufMessageType
{
    /// <summary>
    /// Unknown or unrecognized message type.
    /// </summary>
    Unknown,

    /// <summary>
    /// Status message containing device information and configuration.
    /// </summary>
    Status,

    /// <summary>
    /// Streaming data message containing sensor readings.
    /// </summary>
    Stream,

    /// <summary>
    /// SD card related message (file listings, etc.).
    /// </summary>
    SdCard,

    /// <summary>
    /// Error message from the device.
    /// </summary>
    Error
}
```

### `IInboundMessage` (from `src/Daqifi.Core/Communication/Messages/IInboundMessage.cs`) **[already ported as `daqifi/_internal/comm.py:286`]**

```csharp
public interface IInboundMessage<out T> // Using 'out' for covariance
{
    /// <summary>
    /// Gets the payload data received from the device.
    /// </summary>
    T Data { get; }
} 
```

## Target rationale

Target file already references `ProtobufProtocolHandler` — extending in place. Look for the existing reference and append your change near it.

**Sibling files mapped non-trivially in this subtree:**
- Sibling file `src/Daqifi.Core/Communication/Producers/ScpiMessageProducer.cs` → `daqifi/_internal/scpi.py` (divergent) — consider whether this port belongs alongside that sibling.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 1: `# Module prelude (imports + top-level type definitions)`
      `Ported from upstream ``Daqifi.Core.Device.Protocol.ProtobufProtocolHandler```
      `(``src/Daqifi.Core/Device/Protocol/ProtobufProtocolHandler.cs`` @ e8d84af).`
      `def can_handle(self, message: InboundMessage[Any]) -> bool:`
      `...`
      `async def handle_async(self, message: InboundMessage[Any]) -> None:`
-     line 91: `# Status messages contain device configuration information`
          `if ProtobufProtocolHandler._is_status_message(message):`
          `return ProtobufMessageType.STATUS`
          `if ProtobufProtocolHandler._is_stream_message(message):`
          `return ProtobufMessageType.STREAM`
-     line 102: `# Error messages contain a non-zero device status`
          `if getattr(message, "device_status", 0) != 0:`
          `return ProtobufMessageType.ERROR`
          `return ProtobufMessageType.UNKNOWN`
          `@staticmethod`
          `def _is_status_message(message: DaqifiOutMessage) -> bool:`

**Top-level declarations** (existing module-level symbols — use as structural overview):

_Type definitions:_
- `class IProtocolHandler`

_Implementations:_
- `class ProtobufProtocolHandler`

_Free functions:_
- `def _is_daqifi_out_message`

## ⚠ Name collision: `ProtobufProtocolHandler`

The upstream type `ProtobufProtocolHandler` matches an existing downstream symbol:
- `class ProtobufProtocolHandler` at line 37 of `daqifi/protocol.py`

Consider before patching:
- Rename the upstream port (e.g. add a suffix or namespace) to avoid the clash
- Rename the existing downstream symbol (less ideal — breaks callers)
- Verify whether the existing symbol is actually a different concept and a rename isn't needed at all

```python
"""Protocol handlers for DAQiFi device messages.

Ported from upstream ``Daqifi.Core.Device.Protocol.ProtobufProtocolHandler``
(``src/Daqifi.Core/Device/Protocol/ProtobufProtocolHandler.cs`` @ e8d84af).
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable

from daqifi.communication import InboundMessage
from daqifi.enums import ProtobufMessageType


@runtime_checkable
class IProtocolHandler(Protocol):
    """Structural contract for objects that can dispatch inbound messages."""

    def can_handle(self, message: InboundMessage[Any]) -> bool:
        """Return True if this handler can process ``message``."""
        ...

    async def handle_async(self, message: InboundMessage[Any]) -> None:
        """Process ``message`` asynchronously."""
        ...


# TODO(portomatic): shallow-stub for upstream
# src/Daqifi.Core/Communication/Messages/DaqifiOutMessage.cs — the real protobuf
# class is duck-typed here. Any object exposing the field names referenced
# below (``device_status``, ``digital_port_num``, ``analog_in_port_num``,
# ``analog_out_port_num``, ``msg_time_stamp``, ``analog_in_data``,
# ``analog_in_data_float``, ``digital_data``) is acceptable.
DaqifiOutMessage = Any


class ProtobufProtocolHandler:
    """Handles protobuf-based protocol messages from DAQiFi devices.

    Routes a ``DaqifiOutMessage`` payload to one of four optional callbacks
    based on a content-driven type detection (see :meth:`detect_message_type`).
    """

    def __init__(
        self,
        status_message_handler: Optional[Callable[[DaqifiOutMessage], None]] = None,
        stream_message_handler: Optional[Callable[[DaqifiOutMessage], None]] = None,
        sd_card_message_handler: Optional[Callable[[DaqifiOutMessage], None]] = None,
        error_message_handler: Optional[Callable[[DaqifiOutMessage], None]] = None,
    ) -> None:
        self._status_message_handler = status_message_handler
        self._stream_message_handler = stream_message_handler
        self._sd_card_message_handler = sd_card_message_handler
        self._error_message_handler = error_message_handler

    def can_handle(self, message: InboundMessage[Any]) -> bool:
        """Return True iff ``message.data`` looks like a DaqifiOutMessage.

        Duck-typed: presence of the ``device_status`` attribute is treated as
        the discriminator since the upstream type check (``is DaqifiOutMessage``)
        has no clean Python analog without coupling to a real protobuf class.
        """
        return _is_daqifi_out_message(message.data)

    async def handle_async(self, message: InboundMessage[Any]) -> None:
        """Dispatch ``message.data`` to the matching handler, if any."""
        pb_message = message.data
        if not _is_daqifi_out_message(pb_message):
            return

        message_type = self.detect_message_type(pb_message)

        if message_type is ProtobufMessageType.STATUS:
            if self._status_message_handler is not None:
                self._status_message_handler(pb_message)
        elif message_type is ProtobufMessageType.STREAM:
            if self._stream_message_handler is not None:
                self._stream_message_handler(pb_message)
        elif message_type is ProtobufMessageType.SD_CARD:
            if self._sd_card_message_handler is not None:
                self._sd_card_message_handler(pb_message)
        elif message_type is ProtobufMessageType.ERROR:
            if self._error_message_handler is not None:
                self._error_message_handler(pb_message)
        # ProtobufMessageType.UNKNOWN and any unmapped value: no-op
        # (could log here if needed)

    @staticmethod
    def detect_message_type(message: DaqifiOutMessage) -> ProtobufMessageType:
        """Detect the type of a protobuf message based on its contents."""
        # Status messages contain device configuration information
        if ProtobufProtocolHandler._is_status_message(message):
            return ProtobufMessageType.STATUS

        # Streaming messages contain analog/digital data with timestamps
        if ProtobufProtocolHandler._is_stream_message(message):
            return ProtobufMessageType.STREAM

        # SD card messages are typically text-based and handled separately
        # (placeholder - actual SD card messages come as text responses, not protobuf)

        # Error messages contain a non-zero device status
        if getattr(message, "device_status", 0) != 0:
            return ProtobufMessageType.ERROR

        return ProtobufMessageType.UNKNOWN

    @staticmethod
    def _is_status_message(message: DaqifiOutMessage) -> bool:
        """A message is a status message if it advertises any port count."""
        return (
            getattr(message, "digital_port_num", 0) != 0
            or getattr(message, "analog_in_port_num", 0) != 0
            or getattr(message, "analog_out_port_num", 0) != 0
        )

    @staticmethod
    def _is_stream_message(message: DaqifiOutMessage) -> bool:
        """A message is a stream message if it has a timestamp + payload data.

        USB firmware sends ``analog_in_data_float`` (pre-scaled floats);
        WiFi firmware may send ``analog_in_data`` (raw integer ADC counts).
        Both must be detected. ``digital_data`` is a ``bytes`` blob.
        """
        if getattr(message, "msg_time_stamp", 0) == 0:
            return False

        analog_in_data = getattr(message, "analog_in_data", ()) or ()
        analog_in_data_float = getattr(message, "analog_in_data_float", ()) or ()
        digital_data = getattr(message, "digital_data", b"") or b""

        return (
            len(analog_in_data) > 0
            or len(analog_in_data_float) > 0
            or len(digital_data) > 0
        )


def _is_daqifi_out_message(value: object) -> bool:
    """Duck-type check for a DaqifiOutMessage-shaped object.

    Accepts anything carrying the ``device_status`` field, which is the
    universal discriminator across status / stream / error variants.
    """
    return value is not None and hasattr(value, "device_status")

```

## Convention signals

Usage counts across the downstream tree. Pick the local idiom; zero-usage entries mean introducing this idiom would be a first.

- `@staticmethod`: 18 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 11 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 4 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 16 usages (e.g. `daqifi/enums.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 33 usages (e.g. `daqifi/communication.py`)

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
