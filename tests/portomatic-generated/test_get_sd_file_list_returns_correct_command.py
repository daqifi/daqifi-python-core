"""SCPI vocab consolidation (closes #12).

After the SCPI consolidation, ``scpi_commands.get_sd_file_list()`` is the
single source of truth and emits the canonical SCPI long form that
matches the C# ``ScpiMessageProducer.GetSdFileList`` exactly. The legacy
short-form constant has been removed.
"""
import pytest

from daqifi._internal.scpi_commands import get_sd_file_list


CSHARP_EXPECTED = "SYSTem:STORage:SD:LIST?"


@pytest.mark.portomatic_generated
def test_get_sd_file_list_matches_csharp():
    """Python and C# now emit the same SCPI long form for ``GetSdFileList``."""
    assert get_sd_file_list().data == CSHARP_EXPECTED
