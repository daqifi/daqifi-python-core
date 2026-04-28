# IEEE 488.2 / SCPI command parsing notes

## Short form vs long form
SCPI commands have a long form (e.g. `SYSTem:STORage:SD:LIST?`) and a short
form (e.g. `SYST:STOR:SD:LIST?`). A compliant parser MUST accept both.

The short form is derived by truncating each mnemonic at its uppercase prefix:
- SYSTem → SYST
- STORage → STOR
- LIST → LIST (already short)

## Implication for portomatic DIV-003
Both daqifi-core (long form) and daqifi-python-core (short form, sometimes
mixed-case like `LISt`) emit valid commands. A strict differential equality
test fails, but functional behavior on a compliant device is identical.
