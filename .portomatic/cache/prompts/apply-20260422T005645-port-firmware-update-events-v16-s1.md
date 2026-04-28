# portomatic apply prompt

**Plan:** `20260422T005645-port-firmware-update-events-v16`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: Port FirmwareUpdateProgress + FirmwareUpdateStateChangedEventArgs + FirmwareUpdateException into daqifi/firmware/update_events.py
- classification: new-feature
- shard: Firmware
- upstream_ref: e8d84af:src/Daqifi.Core/Firmware/FirmwareUpdateProgress.cs
- downstream_target: daqifi/firmware/update_events.py

## Port conventions (project-wide)

- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.
- `IProgress<T> progress` → `progress: Callable[[T], None] | None = None` (a plain callback). Call as `progress(value) if progress else None`. The .NET `IProgress<T>` abstraction adds nothing in Python — a callable is enough.

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

In addition: this project has a pytest surface at `tests/portomatic-generated/`. Write unit tests for the ported symbol at `tests/portomatic-generated/test_<slug>.py`; cover all branches and edge cases; run `python3 -m pytest tests/portomatic-generated/test_<slug>.py -q` and include the summary line in your report.

## Upstream file at this commit (full)

```csharp
namespace Daqifi.Core.Firmware;

/// <summary>
/// Progress payload emitted by firmware update operations.
/// </summary>
public sealed class FirmwareUpdateProgress
{
    /// <summary>
    /// Gets or sets the active firmware update state.
    /// </summary>
    public FirmwareUpdateState State { get; set; }

    /// <summary>
    /// Gets or sets the completion percentage in the range 0-100.
    /// </summary>
    public double PercentComplete { get; set; }

    /// <summary>
    /// Gets or sets a human-readable description of the current operation.
    /// </summary>
    public string CurrentOperation { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the number of bytes written so far.
    /// </summary>
    public long BytesWritten { get; set; }

    /// <summary>
    /// Gets or sets the total number of bytes expected for the operation.
    /// </summary>
    public long TotalBytes { get; set; }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

- `FirmwareUpdateState` (from `src/Daqifi.Core/Firmware/FirmwareUpdateState.cs`) **[already ported as `daqifi/enums.py:37`]** _(upstream definition is too large to render compactly; see `src/Daqifi.Core/Firmware/FirmwareUpdateState.cs` for the full contract)_
## Downstream target (current contents)

_File does not exist yet at `daqifi/firmware/update_events.py` — create it._

## Convention signals

Usage counts across the downstream tree. Pick the local idiom; zero-usage entries mean introducing this idiom would be a first.

- `@staticmethod`: 18 usages (e.g. `daqifi/discovery.py`)
- `@classmethod`: 11 usages (e.g. `daqifi/device.py`)
- `Protocol[`: 4 usages (e.g. `daqifi/_internal/comm.py`)
- `abc.ABC`: **0 usages** — introducing this idiom would be a first.
- `@abstractmethod`: **0 usages** — introducing this idiom would be a first.
- `IntEnum`: 21 usages (e.g. `daqifi/enums.py`)
- `StrEnum`: **0 usages** — introducing this idiom would be a first.
- `class …(Enum)`: 2 usages (e.g. `daqifi/discovery.py`)
- `@dataclass`: 35 usages (e.g. `daqifi/communication.py`)

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
