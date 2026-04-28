# portomatic apply prompt

**Plan:** `20260421T200000-port-fwupdateoptions-v15exp`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: Port FirmwareUpdateServiceOptions: configuration knobs for the firmware update service. Used as second-round validation of the v0.15 apply prompt polish (#122-#125). Tests whether friction items reported in the v0.13 head-to-head (#118) actually disappear with the polished prompt template.
- classification: new-feature
- shard: Firmware
- upstream_ref: e8d84af:src/Daqifi.Core/Firmware/FirmwareUpdateServiceOptions.cs
- downstream_target: daqifi/firmware/update_options.py

## Port conventions (project-wide)

- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `TimeSpan` → `float` (seconds) or `datetime.timedelta` if duration arithmetic is needed
- `ArgumentException / ArgumentNullException` → `ValueError` (or `TypeError` for type-mismatch cases)
- `static <Type> Foo => new() {...} (read-only static property of mutable type)` → `@classmethod` factory: `def fast(cls) -> 'Foo': return cls(...)`. Each call returns a fresh instance — module-level singletons would share mutable state across callers. Use this whenever the C# property reads `=> new() {...}` (each access constructs a new object), even if the type's fields look immutable.
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.

## .NET helper idioms

Common BCL helpers that recur across nearly every C#→Python port. Use the listed equivalent rather than re-deriving the translation per call site:

| C# | Python equivalent |
|---|---|
| `string.IsNullOrWhiteSpace(s)` | `not s or not s.strip()` |
| `string.IsNullOrEmpty(s)` | `not s` (works for None and empty string) |
| `nameof(x)` | `"x"` (literal string — common in exception messages and validation) |
| `string.Format("{0}", x)` | `f"{x}"` (f-string) — fall back to `.format()` only when the format string is dynamic |
| `string.Join(",", items)` | `",".join(items)` when items are strings; `",".join(str(x) for x in items)` when not — Python `str.join` raises `TypeError` on non-string elements while .NET `string.Join` stringifies them. Note the order flip — separator first in C#, last in Python. |

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

In addition: this project has a pytest surface at `tests/portomatic-generated/`. Write unit tests for the ported symbol at `tests/portomatic-generated/test_<slug>.py`; cover all branches and edge cases; run `python3 -m pytest tests/portomatic-generated/test_<slug>.py -q` and include the summary line in your report.

## Target symbol: `FirmwareUpdateServiceOptions`

This step ports ONLY `FirmwareUpdateServiceOptions` from the upstream file.

## Upstream file at this commit (full)

```csharp
namespace Daqifi.Core.Firmware;

/// <summary>
/// Configuration options for <see cref="FirmwareUpdateService"/>.
/// </summary>
public sealed class FirmwareUpdateServiceOptions
{
    /// <summary>
    /// DAQiFi PIC32 bootloader USB vendor identifier.
    /// </summary>
    public int BootloaderVendorId { get; set; } = 0x04D8;

    /// <summary>
    /// DAQiFi PIC32 bootloader USB product identifier.
    /// </summary>
    public int BootloaderProductId { get; set; } = 0x003C;

    /// <summary>
    /// Poll interval used while waiting for bootloader and serial re-enumeration.
    /// </summary>
    public TimeSpan PollInterval { get; set; } = TimeSpan.FromMilliseconds(250);

    /// <summary>
    /// Timeout for the <see cref="FirmwareUpdateState.PreparingDevice"/> state.
    /// </summary>
    public TimeSpan PreparingDeviceTimeout { get; set; } = TimeSpan.FromSeconds(20);

    /// <summary>
    /// Timeout for the <see cref="FirmwareUpdateState.WaitingForBootloader"/> state.
    /// </summary>
    public TimeSpan WaitingForBootloaderTimeout { get; set; } = TimeSpan.FromSeconds(45);

    /// <summary>
    /// Timeout for the <see cref="FirmwareUpdateState.Connecting"/> state.
    /// </summary>
    public TimeSpan ConnectingTimeout { get; set; } = TimeSpan.FromSeconds(20);

    /// <summary>
    /// Timeout for the <see cref="FirmwareUpdateState.ErasingFlash"/> state.
    /// </summary>
    public TimeSpan ErasingFlashTimeout { get; set; } = TimeSpan.FromSeconds(45);

    /// <summary>
    /// Timeout for the <see cref="FirmwareUpdateState.Programming"/> state.
    /// </summary>
    public TimeSpan ProgrammingTimeout { get; set; } = TimeSpan.FromMinutes(10);

    /// <summary>
    /// Timeout for the <see cref="FirmwareUpdateState.Verifying"/> state.
    /// </summary>
    public TimeSpan VerifyingTimeout { get; set; } = TimeSpan.FromSeconds(45);

    /// <summary>
    /// Timeout for the <see cref="FirmwareUpdateState.JumpingToApp"/> state.
    /// </summary>
    public TimeSpan JumpingToApplicationTimeout { get; set; } = TimeSpan.FromSeconds(45);

    /// <summary>
    /// Timeout used for individual bootloader read operations.
    /// </summary>
    public TimeSpan BootloaderResponseTimeout { get; set; } = TimeSpan.FromSeconds(10);

    /// <summary>
    /// Delay after sending SCPI FORCEBOOT before disconnecting serial.
    /// </summary>
    public TimeSpan PostForceBootDelay { get; set; } = TimeSpan.FromMilliseconds(1500);

    /// <summary>
    /// Delay after switching LAN firmware update mode before disconnecting serial.
    /// </summary>
    public TimeSpan PostLanFirmwareModeDelay { get; set; } = TimeSpan.FromSeconds(2);

    /// <summary>
    /// Delay before attempting to reconnect serial after WiFi tool execution.
    /// </summary>
    public TimeSpan PostWifiReconnectDelay { get; set; } = TimeSpan.FromSeconds(2);

    /// <summary>
    /// Maximum HID connection attempts during bootloader connect, including the initial attempt.
    /// </summary>
    public int HidConnectRetryCount { get; set; } = 3;

    /// <summary>
    /// Delay between HID connection attempts.
    /// </summary>
    public TimeSpan HidConnectRetryDelay { get; set; } = TimeSpan.FromMilliseconds(500);

    /// <summary>
    /// Maximum programming attempts per flash record, including the initial attempt.
    /// </summary>
    public int FlashWriteRetryCount { get; set; } = 3;

    /// <summary>
    /// Delay between flash record retry attempts.
    /// </summary>
    public TimeSpan FlashWriteRetryDelay { get; set; } = TimeSpan.FromMilliseconds(150);

    /// <summary>
    /// Timeout for the external WiFi flashing process.
    /// </summary>
    public TimeSpan WifiProcessTimeout { get; set; } = TimeSpan.FromMinutes(8);

    /// <summary>
    /// File name used when <c>firmwarePath</c> points to a directory containing WiFi tools.
    /// </summary>
    public string WifiFlashToolFileName { get; set; } = "winc_flash_tool.cmd";

    /// <summary>
    /// Arguments template for WiFi flash tool execution.
    /// Supports <c>{port}</c> and optional <c>{firmwarePath}</c> placeholders.
    /// The default WINC script-based flow discovers firmware artifacts from its working directory
    /// and therefore does not require <c>{firmwarePath}</c>.
    /// </summary>
    public string WifiFlashToolArgumentsTemplate { get; set; } = "/p {port} /d WINC1500 /k /e /i aio /w";

    /// <summary>
    /// Optional explicit serial port override for WiFi updates.
    /// Defaults to <see cref="Device.IDevice.Name"/> when null/empty.
    /// </summary>
    public string? WifiPortOverride { get; set; }

    /// <summary>
    /// Gets the configured timeout for a given firmware update state.
    /// </summary>
    /// <param name="state">The target state.</param>
    /// <returns>The configured timeout.</returns>
    public TimeSpan GetStateTimeout(FirmwareUpdateState state)
    {
        return state switch
        {
            FirmwareUpdateState.PreparingDevice => PreparingDeviceTimeout,
            FirmwareUpdateState.WaitingForBootloader => WaitingForBootloaderTimeout,
            FirmwareUpdateState.Connecting => ConnectingTimeout,
            FirmwareUpdateState.ErasingFlash => ErasingFlashTimeout,
            FirmwareUpdateState.Programming => ProgrammingTimeout,
            FirmwareUpdateState.Verifying => VerifyingTimeout,
            FirmwareUpdateState.JumpingToApp => JumpingToApplicationTimeout,
            _ => TimeSpan.FromMinutes(5)
        };
    }

    /// <summary>
    /// Validates option values and throws when a value is invalid.
    /// </summary>
    public void Validate()
    {
        ValidatePositive(PollInterval, nameof(PollInterval));
        ValidatePositive(PreparingDeviceTimeout, nameof(PreparingDeviceTimeout));
        ValidatePositive(WaitingForBootloaderTimeout, nameof(WaitingForBootloaderTimeout));
        ValidatePositive(ConnectingTimeout, nameof(ConnectingTimeout));
        ValidatePositive(ErasingFlashTimeout, nameof(ErasingFlashTimeout));
        ValidatePositive(ProgrammingTimeout, nameof(ProgrammingTimeout));
        ValidatePositive(VerifyingTimeout, nameof(VerifyingTimeout));
        ValidatePositive(JumpingToApplicationTimeout, nameof(JumpingToApplicationTimeout));
        ValidatePositive(BootloaderResponseTimeout, nameof(BootloaderResponseTimeout));
        ValidatePositive(PostForceBootDelay, nameof(PostForceBootDelay));
        ValidatePositive(PostLanFirmwareModeDelay, nameof(PostLanFirmwareModeDelay));
        ValidatePositive(PostWifiReconnectDelay, nameof(PostWifiReconnectDelay));
        ValidatePositive(HidConnectRetryDelay, nameof(HidConnectRetryDelay));
        ValidatePositive(FlashWriteRetryDelay, nameof(FlashWriteRetryDelay));
        ValidatePositive(WifiProcessTimeout, nameof(WifiProcessTimeout));

        if (HidConnectRetryCount < 1)
        {
            throw new ArgumentOutOfRangeException(
                nameof(HidConnectRetryCount),
                HidConnectRetryCount,
                "HID connect retry count must be at least 1.");
        }

        if (FlashWriteRetryCount < 1)
        {
            throw new ArgumentOutOfRangeException(
                nameof(FlashWriteRetryCount),
                FlashWriteRetryCount,
                "Flash write retry count must be at least 1.");
        }

        if (BootloaderVendorId < 0 || BootloaderVendorId > 0xFFFF)
        {
            throw new ArgumentOutOfRangeException(
                nameof(BootloaderVendorId),
                BootloaderVendorId,
                "Bootloader vendor ID must be in range 0..65535.");
        }

        if (BootloaderProductId < 0 || BootloaderProductId > 0xFFFF)
        {
            throw new ArgumentOutOfRangeException(
                nameof(BootloaderProductId),
                BootloaderProductId,
                "Bootloader product ID must be in range 0..65535.");
        }

        if (string.IsNullOrWhiteSpace(WifiFlashToolFileName))
        {
            throw new ArgumentException("WiFi flash tool file name cannot be empty.", nameof(WifiFlashToolFileName));
        }
    }

    private static void ValidatePositive(TimeSpan value, string parameterName)
    {
        if (value <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(parameterName, value, "Timeouts and delays must be greater than zero.");
        }
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

- `FirmwareUpdateState` (from `src/Daqifi.Core/Firmware/FirmwareUpdateState.cs`) **[already ported as `daqifi/enums.py:37`]** _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Firmware/FirmwareUpdateState.cs` for the full contract)_
**Other referenced types** (upstream definitions are large or unfindable AND no downstream port exists yet — investigate before porting if the call sites use them):
- `BootloaderProductId` _(upstream definition not found)_
- `WifiFlashToolFileName` _(upstream definition not found)_
- `HidConnectRetryCount` _(upstream definition not found)_
- `ID` _(upstream definition not found)_
- `FlashWriteRetryCount` _(upstream definition not found)_
- `WiFi` _(upstream definition not found)_
- `Zero` _(upstream definition not found)_
- `Timeouts` _(upstream definition not found)_
- `HID` _(upstream definition not found)_
- `IsNullOrWhiteSpace` _(upstream definition not found)_

## Downstream target (current contents)

_File does not exist yet at `daqifi/firmware/update_options.py` — create it._

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
- `@dataclass`: 34 usages (e.g. `daqifi/communication.py`)

## Output requirements

- Edit the downstream file (or create it if missing) to implement the upstream change in idiomatic Python.
- Do NOT transliterate — port. Use Python idioms (dataclasses, properties, type hints, exceptions).
- Honour any active divergences listed above (don't 'fix' an intentional divergence).
- **This is a new file.** Apply the project-wide port conventions and the convention-signals shown above; there's no existing local section to mimic.
- For Python smoke checks, use `python3` (auto-detected for this project — venv preferred, else python3).
- After editing, run the verifier policy. Bound at N=3 reflection iterations.
- **On failure (verifier still failing after 3 attempts), STOP and report.** Do NOT call `AskUserQuestion`, do NOT file a sub-ticket yourself, do NOT keep iterating past N=3. Just include in your report:
  - The diff you applied (latest attempt)
  - The verifier output (last ~20 lines)
  - Your best guess at the failure class: `compile`, `behavioral_drift`, `missing_dep`, `flaky_test`, or `unknown`
  - The orchestrator marks the ticket `blocked`, surfaces the failure to the user, and decides skip/retry/abort.
- **DO NOT run `portomatic apply close` yourself** — the orchestrator that dispatched you owns the close step. Just report the diff you applied, whether tests passed, and any blockers. The orchestrator runs `apply close` after verifying.
