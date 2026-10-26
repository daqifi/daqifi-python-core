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
            standard_output_lines=("a", "b"),
            standard_error_lines=(),
        )
        assert res.exit_code == 0
        assert res.timed_out is False
        assert res.duration_seconds == 0.5
        assert res.standard_output_lines == ("a", "b")
        assert res.standard_error_lines == ()

    def test_default_lines_empty(self):
        res = ExternalProcessResult(exit_code=0, timed_out=False, duration_seconds=0.0)
        assert res.standard_output_lines == ()
        assert res.standard_error_lines == ()

    def test_lines_are_tuples_for_immutability(self):
        # Qodo PR #11 pass-2 finding S3: frozen dataclass with mutable
        # default lists is not deeply immutable.
        res = ExternalProcessResult(exit_code=0, timed_out=False, duration_seconds=0.0)
        assert isinstance(res.standard_output_lines, tuple)
        assert isinstance(res.standard_error_lines, tuple)

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


class TestProcessTreeOnTimeout:
    # Audit finding: only the direct child was killed; a descendant holding
    # the output pipes kept run() blocked until it exited (6.1 s for a 6 s
    # child with a 0.3 s timeout), and forever for a hung one.

    _PARENT = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(60)\n"
    )

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process groups")
    def test_timeout_returns_promptly_and_kills_descendants(self):
        import time

        start = time.monotonic()
        result = SubprocessRunner().run(
            ExternalProcessRequest(
                file_name=sys.executable,
                arguments=["-c", self._PARENT],
                timeout_seconds=0.3,
            )
        )
        elapsed = time.monotonic() - start

        assert result.timed_out
        assert elapsed < 3.0


class TestWindowsCommandLine:
    # Audit finding: on Windows a string argument list was shlex-split with
    # posix=False, which keeps quotes, and list2cmdline then escaped them, so
    # '"C:\Firmware Files\x.bin"' arrived with literal quotes. Verified on a
    # real Windows interpreter; this pins the command construction.

    def test_string_arguments_pass_through_verbatim_on_windows(self, monkeypatch):
        from daqifi.firmware import external_process

        monkeypatch.setattr(external_process, "_IS_WINDOWS", True)
        request = ExternalProcessRequest(
            file_name=r"C:\Tools\flash tool.exe",
            arguments=r'/i "C:\Firmware Files\image.bin" /w',
        )
        assert external_process._build_command(request) == (
            r'"C:\Tools\flash tool.exe" /i "C:\Firmware Files\image.bin" /w'
        )

    def test_list_arguments_stay_a_list_on_windows(self, monkeypatch):
        from daqifi.firmware import external_process

        monkeypatch.setattr(external_process, "_IS_WINDOWS", True)
        request = ExternalProcessRequest(file_name="tool.exe", arguments=["/c", "a b"])
        assert external_process._build_command(request) == ["tool.exe", "/c", "a b"]

    def test_string_arguments_are_split_on_posix(self, monkeypatch):
        from daqifi.firmware import external_process

        monkeypatch.setattr(external_process, "_IS_WINDOWS", False)
        request = ExternalProcessRequest(file_name="tool", arguments='-x "a b"')
        assert external_process._build_command(request) == ["tool", "-x", "a b"]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process groups")
class TestDescendantOutput:
    def test_output_from_a_descendant_after_the_launcher_exits_is_captured(self):
        # Audit finding on #166: run() returned when the launcher exited and
        # dropped its worker's later "failed" line, so a failed flash passed.
        script = (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c',"
            " 'import time; time.sleep(6); print(\"Programming device failed\", flush=True)'])\n"
            "print('started', flush=True)\n"
        )
        result = SubprocessRunner().run(
            ExternalProcessRequest(
                file_name=sys.executable, arguments=["-c", script], timeout_seconds=15
            )
        )
        assert not result.timed_out
        assert result.standard_output_lines == ("started", "Programming device failed")

    def test_descendant_outliving_the_budget_is_a_timeout(self):
        import time

        script = (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        )
        start = time.monotonic()
        result = SubprocessRunner().run(
            ExternalProcessRequest(
                file_name=sys.executable, arguments=["-c", script], timeout_seconds=1.0
            )
        )
        assert result.timed_out
        assert time.monotonic() - start < 4.0

    def test_stdin_held_by_an_escaped_descendant_does_not_hang_cleanup(self):
        # Audit finding on #166: the stdout pump blocked writing a response
        # into a pipe a detached descendant held; closing stdin then waited
        # on the writer's lock until that descendant exited.
        import time

        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'],"
            " start_new_session=True)\n"
            "print('prompt', flush=True)\n"
            "time.sleep(60)\n"
        )
        start = time.monotonic()
        result = SubprocessRunner().run(
            ExternalProcessRequest(
                file_name=sys.executable,
                arguments=["-c", script],
                timeout_seconds=0.5,
                standard_input_response_factory=lambda line: "x" * 1_000_000,
            )
        )
        assert result.timed_out
        assert time.monotonic() - start < 5.0


class TestWindowsTreeKill:
    # Audit finding on #166 (round 2): once a launcher has exited,
    # `taskkill /T` can't reach the workers it left holding the pipes. The
    # job object can. Verified on a real Windows interpreter; this pins the
    # order of the kill path on any host.

    class _Proc:
        pid = 4242

        def __init__(self, exited):
            self._exited = exited
            self.killed = False

        def poll(self):
            return 0 if self._exited else None

        def kill(self):
            self.killed = True

    class _Job:
        def __init__(self, ok):
            self._ok = ok
            self.terminated = False

        def terminate(self):
            self.terminated = True
            return self._ok

    def test_job_reaches_workers_after_the_launcher_exited(self, monkeypatch):
        from daqifi.firmware import external_process

        calls = []
        monkeypatch.setattr(external_process, "_IS_WINDOWS", True)
        monkeypatch.setattr(external_process.subprocess, "run", lambda *a, **k: calls.append(a))
        job = self._Job(ok=True)
        external_process._kill_process_tree(self._Proc(exited=True), job)
        assert job.terminated
        assert calls == []

    def test_taskkill_is_the_fallback_without_a_job(self, monkeypatch):
        from daqifi.firmware import external_process

        calls = []
        monkeypatch.setattr(external_process, "_IS_WINDOWS", True)
        monkeypatch.setattr(external_process.subprocess, "run", lambda *a, **k: calls.append(a[0]))
        proc = self._Proc(exited=False)
        external_process._kill_process_tree(proc, self._Job(ok=False))
        assert calls == [["taskkill", "/F", "/T", "/PID", "4242"]]
        assert proc.killed
