"""Tests for `daqifi.firmware.external_process`."""
from __future__ import annotations

import sys

import pytest

from daqifi.firmware import (
    ExternalProcessRequest,
    ExternalProcessResult,
    ExternalProcessRunner,
    SubprocessRunner,
)


# -- ExternalProcessRequest --------------------------------------------

class TestExternalProcessRequest:
    def test_defaults(self):
        req = ExternalProcessRequest()
        assert req.file_name == ""
        assert req.arguments == ""
        assert req.working_directory is None
        assert req.timeout_seconds == 5 * 60.0
        assert req.on_standard_output_line is None
        assert req.on_standard_error_line is None
        assert req.standard_input_response_factory is None

    def test_construct_with_fields(self):
        req = ExternalProcessRequest(
            file_name="echo",
            arguments="hello",
            timeout_seconds=10.0,
        )
        assert req.file_name == "echo"
        assert req.arguments == "hello"
        assert req.timeout_seconds == 10.0

    def test_mutable(self):
        req = ExternalProcessRequest()
        req.file_name = "ls"
        assert req.file_name == "ls"


# -- ExternalProcessResult ---------------------------------------------

class TestExternalProcessResult:
    def test_construct(self):
        res = ExternalProcessResult(
            exit_code=0,
            timed_out=False,
            duration_seconds=0.5,
            standard_output_lines=["a", "b"],
            standard_error_lines=[],
        )
        assert res.exit_code == 0
        assert res.timed_out is False
        assert res.duration_seconds == 0.5
        assert res.standard_output_lines == ["a", "b"]
        assert res.standard_error_lines == []

    def test_default_lines_empty(self):
        res = ExternalProcessResult(exit_code=0, timed_out=False, duration_seconds=0.0)
        assert res.standard_output_lines == []
        assert res.standard_error_lines == []

    def test_is_frozen(self):
        res = ExternalProcessResult(exit_code=0, timed_out=False, duration_seconds=0.0)
        with pytest.raises(Exception):
            res.exit_code = 1  # type: ignore[misc]


# -- SubprocessRunner --------------------------------------------------

class TestSubprocessRunner:
    def test_implements_protocol(self):
        runner: ExternalProcessRunner = SubprocessRunner()
        assert isinstance(runner, ExternalProcessRunner)

    def test_none_request_raises(self):
        runner = SubprocessRunner()
        with pytest.raises(TypeError):
            runner.run(None)  # type: ignore[arg-type]

    def test_empty_filename_raises(self):
        runner = SubprocessRunner()
        with pytest.raises(ValueError):
            runner.run(ExternalProcessRequest(file_name=""))

    def test_runs_python_one_liner(self):
        runner = SubprocessRunner()
        req = ExternalProcessRequest(
            file_name=sys.executable,
            arguments=["-c", "print('hello')"],
            timeout_seconds=10.0,
        )
        result = runner.run(req)
        assert result.exit_code == 0
        assert result.timed_out is False
        assert "hello" in "\n".join(result.standard_output_lines)

    def test_captures_stderr(self):
        runner = SubprocessRunner()
        req = ExternalProcessRequest(
            file_name=sys.executable,
            arguments=["-c", "import sys; sys.stderr.write('oops\\n')"],
            timeout_seconds=10.0,
        )
        result = runner.run(req)
        # The stderr write may or may not include trailing newline depending on
        # buffering — just check it landed somewhere.
        assert any("oops" in line for line in result.standard_error_lines)

    def test_nonzero_exit_code(self):
        runner = SubprocessRunner()
        req = ExternalProcessRequest(
            file_name=sys.executable,
            arguments=["-c", "import sys; sys.exit(42)"],
            timeout_seconds=10.0,
        )
        result = runner.run(req)
        assert result.exit_code == 42
        assert result.timed_out is False

    def test_timeout(self):
        runner = SubprocessRunner()
        req = ExternalProcessRequest(
            file_name=sys.executable,
            arguments=["-c", "import time; time.sleep(5)"],
            timeout_seconds=0.5,
        )
        result = runner.run(req)
        assert result.timed_out is True

    def test_missing_executable_raises(self):
        runner = SubprocessRunner()
        with pytest.raises(FileNotFoundError):
            runner.run(ExternalProcessRequest(file_name="/nonexistent/binary/xyz123"))

    def test_stdout_callback_fires(self):
        runner = SubprocessRunner()
        captured = []
        req = ExternalProcessRequest(
            file_name=sys.executable,
            arguments=["-c", "print('one'); print('two')"],
            on_standard_output_line=captured.append,
            timeout_seconds=10.0,
        )
        result = runner.run(req)
        assert result.exit_code == 0
        assert "one" in captured
        assert "two" in captured

    def test_callback_exception_does_not_kill_run(self):
        runner = SubprocessRunner()

        def bad_callback(line: str) -> None:
            raise RuntimeError("boom")

        req = ExternalProcessRequest(
            file_name=sys.executable,
            arguments=["-c", "print('hello')"],
            on_standard_output_line=bad_callback,
            timeout_seconds=10.0,
        )
        result = runner.run(req)
        # Callback errors are swallowed — process should still complete cleanly.
        assert result.exit_code == 0
