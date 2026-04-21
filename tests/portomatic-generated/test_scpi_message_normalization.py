"""Tests for ScpiMessage outbound text normalization (Qodo PR #11 pass-7 S1)."""
from __future__ import annotations

import pytest

from daqifi._internal.comm import ScpiMessage


class TestScpiMessageGetBytes:
    def test_appends_crlf_when_missing(self):
        assert ScpiMessage(data="*IDN?").get_bytes() == b"*IDN?\r\n"

    def test_does_not_double_terminate_when_caller_already_added_crlf(self):
        # Without rstrip, this would emit "*IDN?\r\n\r\n".
        assert ScpiMessage(data="*IDN?\r\n").get_bytes() == b"*IDN?\r\n"

    def test_strips_solitary_lf(self):
        assert ScpiMessage(data="*IDN?\n").get_bytes() == b"*IDN?\r\n"

    def test_non_ascii_payload_raises_value_error(self):
        # Non-ASCII chars (e.g. accented letters) must surface as ValueError
        # rather than UnicodeEncodeError leaking out from deep in encode().
        with pytest.raises(ValueError, match="ASCII"):
            ScpiMessage(data="café").get_bytes()
