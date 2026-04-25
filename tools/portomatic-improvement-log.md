# Portomatic improvement log

Findings from the daqifi-python-core port. Each entry: a one-line
problem statement, a concrete example, and a suggested fix to take
upstream into the portomatic repo. Numbered for easy cross-reference
in PRs.

## 1. Coverage classifier: single-token name match yields overlap 1.0

**Example:** `IDevice` (interface) → `_internal/comm.py:DeviceType` (enum),
overlap 1.0. They share only the token "Device" — clearly not the same
type. Should be flagged as suspicious or downgraded.

**Fix sketch:** require ≥2 token overlap when both names have ≥2 tokens
(after dropping I-prefix for upstream and IntEnum/Type/Args suffixes for
downstream).

## 2. Coverage classifier: same false-positive pattern across multiple types

**Examples seen in batch 9 alone:**
- `IMessageParser` → `IInboundMessage` (one shared token "Message")
- `ProtobufMessageParser` → `enums.py:ProtobufMessageType` (token-shared,
  but `Type` is an enum suffix)
- `DeviceStatusEventArgs` → `TransportStatusEventArgs` (partial; matches
  on "StatusEventArgs" suffix but the prefix differs entirely)

**Fix sketch:** suffix-aware token weighting — `Type`, `Args`, `Helper`,
`Service`, `Provider` carry less semantic weight than discriminative
prefix tokens.

## 3. I-prefix dropping creates a downstream name collision

**Example:** Python `UdpTransport` (Protocol, ported from C# `IUdpTransport`)
gets matched by *both* upstream `IUdpTransport` AND upstream `UdpTransport`
(concrete class). Coverage then reports `UdpTransport` (concrete) as ported
when it's actually still missing.

**Fix sketch:** track which downstream symbol-and-kind has already been
claimed by an interface match; for the concrete class, prefer a different
downstream candidate or report `partial` rather than `ported`.

## 4. `module-of-functions` strategy doesn't match obvious cases

**Example:** `ScpiMessageProducer` (~40 static factory methods upstream)
vs `daqifi/_internal/scpi_commands.py` (~40 free functions, snake_case).
The static-class → module-of-functions strategy from #137 should fire,
but coverage still reports `ScpiMessageProducer` as missing.

**Fix sketch:** the strategy currently filters on `is_static` per member,
but C# `static` factory *methods returning non-static instances* may not
be emitted as `IRMember.is_static=True` by the tree-sitter extractor.
Verify the extractor catches both `public static IFoo Bar => ...` and
`public static IFoo Bar() => ...`.

## 5. Scaffold: `pytestmark = pytest.mark.X` not registered → warning per file

Every new test file gets `PytestUnknownMarkWarning` for
`portomatic_generated`. The mark works, but the warning noise hides
real issues. Either:
- Generated `conftest.py` should register the mark, or
- The `apply prompt` test-scaffold section should remind the author to
  add the mark to `pyproject.toml [tool.pytest.ini_options].markers`.

## 6. `coverage --json` field naming inconsistency

`portomatic coverage --detail --json` returns entries keyed
`upstream_name` / `upstream_kind` / etc., but at least one earlier
caller (during this session) tried `e['symbol']` and crashed. Either
add `symbol` as an alias or document the schema in the CLI help.

## How to consume this log

When portomatic improvement work resumes, file each item above as a
ticket (`track:quality` label). Verify it's still reproducible against
the latest portomatic main first — coverage classifier rules change
fast.

## Filed tickets (2026-04-22)

The high-value items above have been filed in
`cptkoolbeenz/portomatic`:

- Item #4 (module-of-functions subset check too strict) →
  [portomatic#140](https://github.com/cptkoolbeenz/portomatic/issues/140)
- Items #1 + #2 (token-overlap false positives, suffix weighting) →
  [portomatic#141](https://github.com/cptkoolbeenz/portomatic/issues/141)
- Item #3 (I-prefix interface vs concrete name collision) →
  [portomatic#142](https://github.com/cptkoolbeenz/portomatic/issues/142)
- Item #5 (scaffold mark warning) →
  [portomatic#143](https://github.com/cptkoolbeenz/portomatic/issues/143)
- Item #6 (coverage --json schema alias) — minor; not filed yet.

A separate downstream consolidation ticket lives in this repo:
[daqifi-python-core#12](https://github.com/daqifi/daqifi-python-core/issues/12).
