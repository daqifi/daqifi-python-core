# portomatic apply prompt

**Plan:** `20260420T220004-port-datasample-v11`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.11 test 4: Re-port DataSample — verify ctors are excluded from member match
- classification: new-feature
- shard: Channel
- upstream_ref: e8d84af:src/Daqifi.Core/Channel/DataSample.cs
- downstream_target: daqifi/channels.py

## Port conventions (project-wide)

- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
- `DateTime / DateTimeOffset` → `datetime.datetime` (use `datetime.now(timezone.utc)` for UTC equivalent of `DateTime.UtcNow`)
- `record / record class / record struct (with primary constructor)` → `@dataclass(frozen=True)` (records are value-equal and immutable by default in C#). Each primary-constructor parameter becomes a field; PascalCase names map to snake_case. Use `slots=True` on Python 3.10+. Default values on the C# side become field defaults. For non-primary record bodies (`record Foo { public int A { get; init; } }`), each `init` property becomes a dataclass field.

## .NET helper idioms

Common BCL helpers that recur across nearly every C#→Python port. Use the listed equivalent rather than re-deriving the translation per call site:

| C# | Python equivalent |
|---|---|
| `string.IsNullOrWhiteSpace(s)` | `not s or not s.strip()` |
| `string.IsNullOrEmpty(s)` | `not s` (works for None and empty string) |
| `string.Format("{0}", x)` | `f"{x}"` (f-string) — fall back to `.format()` only when the format string is dynamic |
| `string.Join(",", items)` | `",".join(items)` when items are strings; `",".join(str(x) for x in items)` when not — Python `str.join` raises `TypeError` on non-string elements while .NET `string.Join` stringifies them. Note the order flip — separator first in C#, last in Python. |

## Verifiers

Verifiers: none configured — smoke-import the changed module only.

Use `python3 -c "import <module.path>"` and report the result. No `harness run` invocation needed.

## Upstream file at this commit (full)

```csharp
namespace Daqifi.Core.Channel;

/// <summary>
/// Represents a single data sample from a channel with timestamp and value.
/// </summary>
public class DataSample : IDataSample
{
    /// <summary>
    /// Gets the timestamp when the sample was taken.
    /// </summary>
    public DateTime Timestamp { get; init; }

    /// <summary>
    /// Gets or sets the value of the sample.
    /// </summary>
    public double Value { get; set; }

    /// <summary>
    /// Initializes a new instance of the <see cref="DataSample"/> class.
    /// </summary>
    public DataSample()
    {
        Timestamp = DateTime.UtcNow;
        Value = 0.0;
    }

    /// <summary>
    /// Initializes a new instance of the <see cref="DataSample"/> class with specified values.
    /// </summary>
    /// <param name="timestamp">The timestamp when the sample was taken.</param>
    /// <param name="value">The value of the sample.</param>
    public DataSample(DateTime timestamp, double value)
    {
        Timestamp = timestamp;
        Value = value;
    }

    /// <summary>
    /// Returns a string representation of the data sample.
    /// </summary>
    /// <returns>A string containing the timestamp and value.</returns>
    public override string ToString()
    {
        return $"{Timestamp:yyyy-MM-dd HH:mm:ss.fff} - {Value}";
    }
}

```

## Referenced types (1-hop closure)

These types appear in the upstream file's signatures. Their definitions are included so you can see the contract closure. Entries marked **[already ported]** are downstream symbols you can reuse rather than re-create. Entries marked **[base contract]** are interfaces or classes the port MUST conform to.

### `IDataSample` **[base contract]** (from `src/Daqifi.Core/Channel/IDataSample.cs`) **[already ported as `daqifi/_internal/comm.py:158`]**

```csharp
public interface IDataSample
{
    /// <summary>
    /// Gets the timestamp when the sample was taken.
    /// </summary>
    DateTime Timestamp { get; }

    /// <summary>
    /// Gets or sets the value of the sample.
    /// </summary>
    double Value { get; set; }
}
```

- **Existing downstream implementations** (follow this pattern):
  - `class DataSample` at `daqifi/_internal/comm.py:191` _(by name match)_
## Target rationale

Target file has 7 top-level declarations already — extending an established module. Match its existing structure.

**Adjacent divergences in this subtree:**
- Active divergence **DIV-001** covers `src/Daqifi.Core/Channel` (same subtree): _Python port architecturally chooses to delegate scaling/calibration_ — check before porting.

**Sibling files mapped non-trivially in this subtree:**
- Sibling file `src/Daqifi.Core/Channel/AnalogChannel.cs` → `daqifi/channels.py` (divergent): `C# AnalogChannel is a self-contained data model with local` — consider whether this port belongs alongside that sibling.
- Sibling file `src/Daqifi.Core/Channel/IAnalogChannel.cs` → `(none)` (dropped): `Python uses duck typing; no separate interface file.` — consider whether this port belongs alongside that sibling.
- Sibling file `src/Daqifi.Core/Channel/DigitalChannel.cs` → `daqifi/channels.py` (divergent): `Same as AnalogChannel — Python DigitalIOChannel is SCPI-delegated.` — consider whether this port belongs alongside that sibling.

## Downstream target (current contents)

**File regions** (look for the section your change belongs to and match its LOCAL convention — sections often diverge):

- line 1: `# Module prelude (imports + top-level type definitions)`
      `Channel classes for DAQiFi Nyquist devices.`
      `Provides property-based access to analog and digital channels.`
      `Single analog output (DAC) channel.`
      `Provides voltage control and enable/disable functionality.`
      `Available on NQ3 variant only.`
-       line 191: `# Convert string values to 0/1 if needed`
            `if value.upper() in ("5V", "±5V", "+/-5V"):`
            `range_val = 0`
            `elif value.upper() in ("10V", "±10V", "+/-10V"):`
            `range_val = 1`
            `else:`
-       line 279: `# Read entire port direction (16-bit mask)`
            `response = self._comm.send_query(scpi.DigitalIO.PORT_DIRECTION_QUERY)`
            `port_dir = scpi.parse_int(response)`
            `is_output = bool(port_dir & (1 << self._channel))`
            `return "output" if is_output else "input"`
            `except Exception as e:`
-       line 305: `# Read current port direction`
            `response = self._comm.send_query(scpi.DigitalIO.PORT_DIRECTION_QUERY)`
            `port_dir = scpi.parse_int(response)`
-       line 309: `# Modify bit for this channel`
            `if value_lower == "output":`
            `port_dir |= (1 << self._channel)  # Set bit`
            `else:`
            `port_dir &= ~(1 << self._channel)  # Clear bit`
-       line 315: `# Write back entire port direction`
            `success = self._comm.send_set(scpi.DigitalIO.PORT_DIRECTION_SET, port_dir)`
            `if not success:`
            `error = self._comm.send_query(scpi.System.ERR)`
            `raise CommandError(f"Failed to set direction: {error}")`
            `except Exception as e:`
-       line 335: `# Read entire port state (16-bit mask)`
            `response = self._comm.send_query(scpi.DigitalIO.PORT_STATE_QUERY)`
            `port_state = scpi.parse_int(response)`
            `is_high = bool(port_state & (1 << self._channel))`
            `return is_high`
            `except Exception as e:`
-       line 360: `# Read current port state`
            `response = self._comm.send_query(scpi.DigitalIO.PORT_STATE_QUERY)`
            `port_state = scpi.parse_int(response)`
-       line 364: `# Modify bit for this channel`
            `if value:`
            `port_state |= (1 << self._channel)  # Set bit`
            `else:`
            `port_state &= ~(1 << self._channel)  # Clear bit`
-       line 370: `# Write back entire port state`
            `success = self._comm.send_set(scpi.DigitalIO.PORT_STATE_SET, port_state)`
            `if not success:`
            `error = self._comm.send_query(scpi.System.ERR)`
            `raise CommandError(f"Failed to set state: {error}")`
            `except Exception as e:`
- line 387: `# Channel collection classes`
      `class ChannelCollection:`
      `def __init__(self, channels: List):`
      `self._channels = channels`
      `def __getitem__(self, index: int):`
      `if 0 <= index < len(self._channels):`

**Top-level declarations** (existing module-level symbols — use as structural overview):

- `class AnalogOutputChannel`
- `class AnalogInputChannel`
- `class DigitalIOChannel`
- `class ChannelCollection`
- `class AnalogOutputs`
- `class AnalogInputs`
- `class DigitalIO`

**Nearest top-level declarations** (where new code typically lands):

- `class AnalogOutputs`
- `class AnalogInputs`
- `class DigitalIO`

_(full body elided — downstream target is 539 lines and the upstream change is small (46 changed lines); see the regions index + top-level declarations above for structural overview, and `daqifi/channels.py` for the full current file.)_

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

## Map shard `Channel` — relevant entries


### Architectural context (no exact file match)

_Nearest file mappings in this shard — read these BEFORE deciding whether to create a downstream file:_

- `src/Daqifi.Core/Channel/AnalogChannel.cs` → `daqifi/channels.py` (divergent)
  > C# AnalogChannel is a self-contained data model with local
  > scaling/calibration math (GetScaledValue computes (raw / resolution
  > * port_range * cal_m + cal_b) * internal_scale_m client-side).
  > Python AnalogInputChannel is a thin SCPI wrapper with no scaling
  > math — the device computes scaled values and the Python class
  > delegates everything via send_query/send_set. There is no Python
  > class with field-level parity to C# AnalogChannel and no plan to
  > add one (the Python port is intentionally device-delegated).
- `src/Daqifi.Core/Channel/IAnalogChannel.cs` → `(none)` (dropped)
  > Python uses duck typing; no separate interface file.
- `src/Daqifi.Core/Channel/DigitalChannel.cs` → `daqifi/channels.py` (divergent)
  > Same as AnalogChannel — Python DigitalIOChannel is SCPI-delegated.

**Active divergences in this area:**
- `DIV-001` (behavioral): Python port architecturally chooses to delegate scaling/calibration
to the device firmware rather than recomputing client-side. This
means C# tests like GetScaledValue_*, Constructor_*, Properties_*,
ToString_ReturnsChannelName have NO Python equivalent and should
not be ported. New C# AnalogChannel features that touch scaling
math should also be classified as `divergent` on /portomatic-check
until the Python architecture changes.


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
