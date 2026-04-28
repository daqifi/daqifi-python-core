# portomatic apply prompt

**Plan:** `20260421T050001-port-firmwareversion-v13`  ·  **Step:** `s1`
**Project:** daqifi-core-to-daqifi-python-core
**Architecture mode:** free — port idiomatically; structural divergence from upstream is encouraged when it's more Pythonic

## Step

- summary: v0.13 test 1: FirmwareVersion (readonly record struct, IComparable<FirmwareVersion>, operator <, >, <=, >=)
- classification: new-feature
- shard: Firmware
- upstream_ref: e8d84af:src/Daqifi.Core/Firmware/FirmwareVersion.cs
- downstream_target: daqifi/firmware/version.py

## Port conventions (project-wide)

- `public interface IFoo` → `typing.Protocol` (structural) when there are existing or planned conformers; otherwise omit and rely on duck typing
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
using System.Globalization;
using System.Text.RegularExpressions;

namespace Daqifi.Core.Firmware;

/// <summary>
/// Represents a parsed firmware version with semantic versioning and pre-release support.
/// </summary>
public readonly record struct FirmwareVersion(
    int Major,
    int Minor,
    int Patch,
    string? PreLabel,
    int PreNumber) : IComparable<FirmwareVersion>
{
    private static readonly Regex VersionRegex = new(
        """
        (?ix)^\s* v?\s*
                  (?<maj>\d+) (?:\.(?<min>\d+))? (?:\.(?<pat>\d+))?
                  (?<suffix> [A-Za-z]+ \d* )?
                  \s*$
        """,
        RegexOptions.Compiled);

    /// <summary>
    /// Whether this version is a pre-release (alpha, beta, rc, etc.).
    /// </summary>
    public bool IsPreRelease => !string.IsNullOrEmpty(PreLabel);

    /// <inheritdoc />
    public int CompareTo(FirmwareVersion other)
    {
        var cmp = Major.CompareTo(other.Major);
        if (cmp != 0) return cmp;
        cmp = Minor.CompareTo(other.Minor);
        if (cmp != 0) return cmp;
        cmp = Patch.CompareTo(other.Patch);
        if (cmp != 0) return cmp;

        var thisRank = GetPrecedenceRank(PreLabel);
        var otherRank = GetPrecedenceRank(other.PreLabel);
        if (thisRank != otherRank) return thisRank.CompareTo(otherRank);
        return PreNumber.CompareTo(other.PreNumber);
    }

    /// <summary>
    /// Attempts to parse a version string (e.g. "v3.2.0", "3.2.0b2", "v1.0.0rc1").
    /// </summary>
    public static bool TryParse(string? input, out FirmwareVersion version)
    {
        version = default;
        if (string.IsNullOrWhiteSpace(input)) return false;

        var m = VersionRegex.Match(input.Trim());
        if (!m.Success) return false;

        var major = int.Parse(m.Groups["maj"].Value, CultureInfo.InvariantCulture);
        var minor = int.TryParse(m.Groups["min"].Value, out var mi) ? mi : 0;
        var patch = int.TryParse(m.Groups["pat"].Value, out var pa) ? pa : 0;

        var suffix = m.Groups["suffix"].Success ? m.Groups["suffix"].Value : null;
        string? label = null;
        var preNum = 0;
        if (!string.IsNullOrEmpty(suffix))
        {
            var i = suffix.TakeWhile(char.IsLetter).Count();
            label = suffix[..i];
            var numPart = suffix[i..];
            _ = int.TryParse(numPart, out preNum);
        }

        version = new FirmwareVersion(major, minor, patch, label, preNum);
        return true;
    }

    /// <summary>
    /// Compares two version strings. Returns negative if left is older, positive if newer, zero if equal.
    /// Unparseable strings sort before valid versions.
    /// </summary>
    public static int Compare(string? left, string? right)
    {
        var hasL = TryParse(left, out var l);
        var hasR = TryParse(right, out var r);
        if (!hasL && !hasR) return 0;
        if (!hasL) return -1;
        if (!hasR) return 1;
        return l.CompareTo(r);
    }

    /// <inheritdoc />
    public override string ToString()
    {
        var core = $"{Major}.{Minor}.{Patch}";
        if (!IsPreRelease) return core;
        return core + PreLabel + (PreNumber > 0 ? PreNumber.ToString(CultureInfo.InvariantCulture) : string.Empty);
    }

    /// <inheritdoc />
    public static bool operator <(FirmwareVersion left, FirmwareVersion right) => left.CompareTo(right) < 0;
    /// <inheritdoc />
    public static bool operator >(FirmwareVersion left, FirmwareVersion right) => left.CompareTo(right) > 0;
    /// <inheritdoc />
    public static bool operator <=(FirmwareVersion left, FirmwareVersion right) => left.CompareTo(right) <= 0;
    /// <inheritdoc />
    public static bool operator >=(FirmwareVersion left, FirmwareVersion right) => left.CompareTo(right) >= 0;

    private static int GetPrecedenceRank(string? label)
    {
        if (string.IsNullOrEmpty(label)) return 3; // release is highest
        return label.ToLowerInvariant() switch
        {
            "rc" or "releasecandidate" => 2,
            "b" or "beta" => 1,
            "a" or "alpha" or "pre" or "preview" or "dev" => 0,
            _ => 0
        };
    }
}

```

## Referenced types (1-hop closure)

**Other referenced types** (upstream definitions are large or unfindable AND no downstream port exists yet — investigate before porting if the call sites use them):
- `Regex` _(upstream definition not found)_

## Downstream target (current contents)

_File does not exist yet at `daqifi/firmware/version.py` — create it._

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
