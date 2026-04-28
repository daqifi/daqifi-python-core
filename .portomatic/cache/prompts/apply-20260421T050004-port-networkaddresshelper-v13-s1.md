# portomatic apply prompt

**Plan:** `20260421T050004-port-networkaddresshelper-v13`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.13 test 4: NetworkAddressHelper (static class, decodes IPv4/MAC/subnet/gateway/DNS from ByteString)
- classification: new-feature
- shard: Device
- upstream_ref: e8d84af:src/Daqifi.Core/Device/Network/NetworkAddressHelper.cs
- downstream_target: daqifi/network/address_helper.py

## Port conventions (project-wide)

- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `Exception (base type or unspecified subtype)` → Choice depends on direction (#64): **raised** errors → `Exception` or specific subtype (never raise BaseException). **caught/stored** errors (event-args fields, error callbacks, `except` blocks that need to surface anything including KeyboardInterrupt/SystemExit) → `BaseException`. Match upstream semantic intent — `Exception?` field that stores any caught error → `Optional[BaseException]`; method that raises a validation error → raise `Exception` subtype.
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `Transitive un-ported type (referenced inside a referenced type)` → Shallow-stub: type the field/parameter as the closest available concrete type (the generic T parameter, `Any`, or a Protocol with the bare-minimum signature). Add a TODO comment citing the upstream type. Don't recursively port — that's a separate plan step.
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.

## .NET helper idioms

Common BCL helpers that recur across nearly every C#→Python port. Use the listed equivalent rather than re-deriving the translation per call site:

| C# | Python equivalent |
|---|---|
| `string.IsNullOrWhiteSpace(s)` | `not s or not s.strip()` |
| `string.IsNullOrEmpty(s)` | `not s` (works for None and empty string) |
| `string.Format("{0}", x)` | `f"{x}"` (f-string) — fall back to `.format()` only when the format string is dynamic |
| `string.Join(",", items)` | `",".join(items)` when items are strings; `",".join(str(x) for x in items)` when not — Python `str.join` raises `TypeError` on non-string elements while .NET `string.Join` stringifies them. Note the order flip — separator first in C#, last in Python. |
| `Convert.ToInt32 / ToDouble / ToBoolean` | For non-null inputs: `int(...)` / `float(...)` / `bool(...)`. Caveats: `bool('false')` is `True` in Python (any non-empty string is truthy — use `s.lower() == 'true'`); and `Convert.ToInt32(null)` returns `0` in C# while `int(None)` raises `TypeError` — use `0 if x is None else int(x)` when the upstream value can be null. |

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
using Google.Protobuf;

namespace Daqifi.Core.Device.Network;

/// <summary>
/// Provides utility methods for decoding network address fields from <see cref="DaqifiOutMessage"/> protobuf messages.
/// </summary>
public static class NetworkAddressHelper
{
    private const int Ipv4ByteLength = 4;
    private const int MacByteLength = 6;

    /// <summary>
    /// Gets the IP address as a dotted-decimal string (e.g., "192.168.1.100") from the message's <see cref="DaqifiOutMessage.IpAddr"/> field.
    /// </summary>
    /// <param name="message">The protobuf message containing device information.</param>
    /// <returns>The formatted IP address string, or <see cref="string.Empty"/> if the field is empty or malformed.</returns>
    /// <exception cref="ArgumentNullException">Thrown when <paramref name="message"/> is <c>null</c>.</exception>
    public static string GetIpAddressString(DaqifiOutMessage message)
    {
        ArgumentNullException.ThrowIfNull(message);
        return FormatAsIpv4Address(message.IpAddr);
    }

    /// <summary>
    /// Gets the MAC address as a hyphen-separated hex string (e.g., "AA-BB-CC-DD-EE-FF") from the message's <see cref="DaqifiOutMessage.MacAddr"/> field.
    /// </summary>
    /// <param name="message">The protobuf message containing device information.</param>
    /// <returns>The formatted MAC address string, or <see cref="string.Empty"/> if the field is empty or malformed.</returns>
    /// <exception cref="ArgumentNullException">Thrown when <paramref name="message"/> is <c>null</c>.</exception>
    public static string GetMacAddressString(DaqifiOutMessage message)
    {
        ArgumentNullException.ThrowIfNull(message);
        return message.MacAddr.Length == MacByteLength
            ? BitConverter.ToString(message.MacAddr.ToByteArray())
            : string.Empty;
    }

    /// <summary>
    /// Gets the subnet mask as a dotted-decimal string (e.g., "255.255.255.0") from the message's <see cref="DaqifiOutMessage.NetMask"/> field.
    /// </summary>
    /// <param name="message">The protobuf message containing device information.</param>
    /// <returns>The formatted subnet mask string, or <see cref="string.Empty"/> if the field is empty or malformed.</returns>
    /// <exception cref="ArgumentNullException">Thrown when <paramref name="message"/> is <c>null</c>.</exception>
    public static string GetSubnetMaskString(DaqifiOutMessage message)
    {
        ArgumentNullException.ThrowIfNull(message);
        return FormatAsIpv4Address(message.NetMask);
    }

    /// <summary>
    /// Gets the gateway address as a dotted-decimal string (e.g., "192.168.1.1") from the message's <see cref="DaqifiOutMessage.Gateway"/> field.
    /// </summary>
    /// <param name="message">The protobuf message containing device information.</param>
    /// <returns>The formatted gateway address string, or <see cref="string.Empty"/> if the field is empty or malformed.</returns>
    /// <exception cref="ArgumentNullException">Thrown when <paramref name="message"/> is <c>null</c>.</exception>
    public static string GetGatewayString(DaqifiOutMessage message)
    {
        ArgumentNullException.ThrowIfNull(message);
        return FormatAsIpv4Address(message.Gateway);
    }

    /// <summary>
    /// Gets the primary DNS address as a dotted-decimal string (e.g., "8.8.8.8") from the message's <see cref="DaqifiOutMessage.PrimaryDns"/> field.
    /// </summary>
    /// <param name="message">The protobuf message containing device information.</param>
    /// <returns>The formatted primary DNS address string, or <see cref="string.Empty"/> if the field is empty or malformed.</returns>
    /// <exception cref="ArgumentNullException">Thrown when <paramref name="message"/> is <c>null</c>.</exception>
    public static string GetPrimaryDnsString(DaqifiOutMessage message)
    {
        ArgumentNullException.ThrowIfNull(message);
        return FormatAsIpv4Address(message.PrimaryDns);
    }

    /// <summary>
    /// Gets the secondary DNS address as a dotted-decimal string (e.g., "1.1.1.1") from the message's <see cref="DaqifiOutMessage.SecondaryDns"/> field.
    /// </summary>
    /// <param name="message">The protobuf message containing device information.</param>
    /// <returns>The formatted secondary DNS address string, or <see cref="string.Empty"/> if the field is empty or malformed.</returns>
    /// <exception cref="ArgumentNullException">Thrown when <paramref name="message"/> is <c>null</c>.</exception>
    public static string GetSecondaryDnsString(DaqifiOutMessage message)
    {
        ArgumentNullException.ThrowIfNull(message);
        return FormatAsIpv4Address(message.SecondaryDns);
    }

    /// <summary>
    /// Formats a <see cref="ByteString"/> as a dotted-decimal IPv4 address string.
    /// Returns <see cref="string.Empty"/> if the data is not exactly 4 bytes.
    /// </summary>
    private static string FormatAsIpv4Address(ByteString data)
    {
        return data.Length == Ipv4ByteLength
            ? string.Join(".", data.ToByteArray())
            : string.Empty;
    }
}

```

## Referenced types (1-hop closure)

**Other referenced types** (upstream definitions are large or unfindable AND no downstream port exists yet — investigate before porting if the call sites use them):
- `ByteString` _(upstream definition not found)_
- `DaqifiOutMessage` → `src/Daqifi.Core/Communication/Messages/DaqifiOutMessage.cs`

## Downstream target (current contents)

_File does not exist yet at `daqifi/network/address_helper.py` — create it._

## Convention signals

Usage counts across the downstream tree. Pick the local idiom; zero-usage entries mean introducing this idiom would be a first.

- `@staticmethod`: 10 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 10 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 4 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 9 usages (e.g. `daqifi/_internal/comm.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 18 usages (e.g. `daqifi/discovery.py`)

## Research hits (cached docs containing the shard name)

- `scpi_protocol/ieee-488-2-scpi-command-parsing-rules.md:15`: test fails, but functional behavior on a compliant device is identical.

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
