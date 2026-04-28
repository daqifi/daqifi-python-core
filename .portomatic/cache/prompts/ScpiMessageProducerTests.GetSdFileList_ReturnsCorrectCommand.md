# portomatic differential generation prompt

## Goal

Translate the behavioral specification below into a runnable `pytest` test that exercises the **downstream** Python symbol equivalently to the upstream xUnit test. The downstream test must call the same conceptual operations, with the same inputs, and assert the same outcomes — adapted to Python idioms.

## Subject mapping

- Upstream test: `GetSdFileList_ReturnsCorrectCommand` (file: `/mnt/c/Users/User/Documents/GitHub/daqifi-core/src/Daqifi.Core.Tests/Communication/Producers/ScpiMessageProducerTests.cs`)
- Upstream subject: `ScpiMessageProducer`
- Downstream subject (qualified): `daqifi._internal.scpi.System`
- Downstream import: `from daqifi._internal.scpi import System`

### Mapping notes

Python uses SCPI short form (SYST:STOR:SD:LISt?) on a flat constants-class layout; C# uses long form (SYSTem:STORage:SD:LIST?). See map shard Communication.yaml for divergence rationale.

## xUnit decorators

```
Fact
```

## Behavioral spec

### Setup (Arrange)
```csharp
var message = ScpiMessageProducer.GetSdFileList;
AssertMessageFormat(message);
```

### Action (Act)
```csharp
```

### Expected (Assert)
```csharp
Assert.Equal("SYSTem:STORage:SD:LIST?", message.Data);
```

## Raw upstream test body (for reference)

```csharp
{
        var message = ScpiMessageProducer.GetSdFileList;
        Assert.Equal("SYSTem:STORage:SD:LIST?", message.Data);
        AssertMessageFormat(message);
    }
```

## Output requirements

- Replace the skeleton's `pytest.mark.skip` with a real assertion-driven test.
- Use `pytest.mark.parametrize` if InlineData rows were present.
- Floating-point comparisons should use `pytest.approx(expected, rel=1e-6, abs=1e-9)`   unless the spec asserts exact equality.
- Preserve event/callback semantics by registering a list and asserting on it.
- Do not import anything from the upstream language; this is a pure Python test.
