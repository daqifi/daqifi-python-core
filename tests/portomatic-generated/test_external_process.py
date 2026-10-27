"""Tests for `daqifi.firmware.external_process`."""
from __future__ import annotations

import os
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

    def test_job_then_taskkill_for_a_live_launcher(self, monkeypatch):
        from daqifi.firmware import external_process

        order = []
        monkeypatch.setattr(external_process, "_IS_WINDOWS", True)
        monkeypatch.setattr(
            external_process.subprocess, "run", lambda *a, **k: order.append("taskkill")
        )

        class _OrderedJob(self._Job):
            def terminate(inner):
                order.append("job")
                return super().terminate()

        proc = self._Proc(exited=False)
        external_process._kill_process_tree(proc, _OrderedJob(ok=True))
        assert order == ["job", "taskkill"]
        assert proc.killed

    @pytest.mark.parametrize(
        "error", [PermissionError("blocked by policy"), FileNotFoundError("taskkill")]
    )
    def test_a_failing_taskkill_skips_nothing_else(self, monkeypatch, error):
        # Audit finding on #166 (round 4): taskkill raising jumped past the
        # job termination, leaving workers alive.
        from daqifi.firmware import external_process

        def _raise(*a, **k):
            raise error

        monkeypatch.setattr(external_process, "_IS_WINDOWS", True)
        monkeypatch.setattr(external_process.subprocess, "run", _raise)
        proc = self._Proc(exited=False)
        job = self._Job(ok=True)
        external_process._kill_process_tree(proc, job)
        assert job.terminated
        assert proc.killed

    def test_taskkill_alone_without_a_job(self, monkeypatch):
        from daqifi.firmware import external_process

        calls = []
        monkeypatch.setattr(external_process, "_IS_WINDOWS", True)
        monkeypatch.setattr(external_process.subprocess, "run", lambda *a, **k: calls.append(a[0]))
        proc = self._Proc(exited=False)
        external_process._kill_process_tree(proc, None)
        assert calls == [["taskkill", "/F", "/T", "/PID", "4242"]]
        assert proc.killed

    @pytest.mark.parametrize("error", [FileNotFoundError("nope"), PermissionError("denied")])
    def test_job_is_closed_when_the_child_fails_to_start(self, monkeypatch, error):
        # Audit finding on #166 (round 5): a failed launch leaked the job's
        # kernel handle, one per retry.
        from daqifi.firmware import external_process

        closed = []

        class _FakeJob:
            available = True

            def close(self):
                closed.append(True)

        def _fail(*a, **k):
            raise error

        monkeypatch.setattr(external_process, "_IS_WINDOWS", True)
        monkeypatch.setattr(external_process, "_WindowsJob", _FakeJob)
        monkeypatch.setattr(external_process.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
        monkeypatch.setattr(external_process.subprocess, "Popen", _fail)
        with pytest.raises(type(error)):
            SubprocessRunner().run(ExternalProcessRequest(file_name="flash.exe"))
        assert closed == [True]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX zombies")
def test_child_is_reaped_when_the_wait_is_interrupted(monkeypatch):
    # Audit finding on #166 (round 6): on KeyboardInterrupt the child was
    # killed but never waited for, so it stayed a zombie for as long as the
    # caller kept the exception.
    import subprocess as sp

    from daqifi.firmware import external_process

    started = []
    real_popen = sp.Popen

    class _InterruptedPopen(real_popen):
        def wait(self, timeout=None):
            if not started:
                started.append(self)
                raise KeyboardInterrupt
            return super().wait(timeout)

    monkeypatch.setattr(external_process.subprocess, "Popen", _InterruptedPopen)
    with pytest.raises(KeyboardInterrupt) as kept:
        SubprocessRunner().run(
            ExternalProcessRequest(
                file_name=sys.executable,
                arguments=["-c", "import time; time.sleep(60)"],
                timeout_seconds=30,
            )
        )
    assert kept.value is not None
    assert started[0].returncode is not None  # reaped


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX threads/signals")
class TestSetupInsideCleanup:
    # Audit finding on #166 (round 7): an interrupt or a failed thread start
    # between Popen and the cleanup scope left the child running.

    def test_failed_reader_start_kills_and_reaps_the_child(self, monkeypatch):
        import subprocess as sp
        import threading as th

        from daqifi.firmware import external_process

        spawned = []
        real_popen = sp.Popen

        class _RecordingPopen(real_popen):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                spawned.append(self)

        def _no_threads(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(external_process.subprocess, "Popen", _RecordingPopen)
        monkeypatch.setattr(th.Thread, "start", _no_threads)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            SubprocessRunner().run(
                ExternalProcessRequest(
                    file_name=sys.executable,
                    arguments=["-c", "import time; time.sleep(60)"],
                    timeout_seconds=30,
                )
            )
        assert spawned[0].returncode is not None
        assert spawned[0].stdout.closed and spawned[0].stderr.closed

    def test_escaped_worker_output_after_return_reaches_nobody(self):
        # Audit finding on #166 (round 7): after a timeout, a worker outside
        # the process group kept firing the caller's progress callback.
        import time

        seen = []
        script = (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c',"
            " 'import time\\nfor i in range(100):\\n    print(i, flush=True); time.sleep(0.1)'],"
            " start_new_session=True)\n"
        )
        result = SubprocessRunner().run(
            ExternalProcessRequest(
                file_name=sys.executable,
                arguments=["-c", script],
                timeout_seconds=0.5,
                on_standard_output_line=seen.append,
            )
        )
        assert result.timed_out
        count_at_return = len(seen)
        lines_at_return = len(result.standard_output_lines)
        time.sleep(1.0)
        assert len(seen) == count_at_return
        assert lines_at_return == len(result.standard_output_lines)

    def test_pipe_of_a_reader_that_never_started_is_closed(self, monkeypatch):
        # Audit finding on #166 (round 8): stdout's reader started, stderr's
        # didn't, and the stderr pipe was left open.
        import subprocess as sp
        import threading as th

        from daqifi.firmware import external_process

        spawned = []
        real_popen = sp.Popen
        real_start = th.Thread.start
        starts = []

        class _RecordingPopen(real_popen):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                spawned.append(self)

        def _second_start_fails(self):
            starts.append(self)
            if len(starts) == 2:
                raise RuntimeError("can't start new thread")
            return real_start(self)

        monkeypatch.setattr(external_process.subprocess, "Popen", _RecordingPopen)
        monkeypatch.setattr(th.Thread, "start", _second_start_fails)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            SubprocessRunner().run(
                ExternalProcessRequest(
                    file_name=sys.executable,
                    arguments=["-c", "import time; time.sleep(60)"],
                    timeout_seconds=30,
                )
            )
        assert spawned[0].returncode is not None
        assert spawned[0].stderr.closed

    def test_interrupt_right_after_the_child_starts_kills_and_reaps_it(self, monkeypatch):
        # Audit finding on #166 (round 8): setup between Popen returning and
        # the cleanup scope (creating the reader Event) could be interrupted,
        # leaving the child running. Any setup that still runs after the
        # child exists raises here; either way the child must end up reaped.
        import subprocess as sp
        import threading as th

        from daqifi.firmware import external_process

        spawned = []
        real_popen = sp.Popen
        real_event = th.Event

        class _RecordingPopen(real_popen):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                spawned.append(self)

        def _event_after_spawn_interrupts(*a, **k):
            if spawned:
                raise KeyboardInterrupt
            return real_event(*a, **k)

        monkeypatch.setattr(external_process.subprocess, "Popen", _RecordingPopen)
        monkeypatch.setattr(external_process.threading, "Event", _event_after_spawn_interrupts)
        try:
            SubprocessRunner().run(
                ExternalProcessRequest(
                    file_name=sys.executable,
                    arguments=["-c", "import time; time.sleep(60)"],
                    timeout_seconds=1,
                )
            )
        except KeyboardInterrupt:
            pass
        assert spawned[0].returncode is not None

    def test_interrupt_just_after_a_reader_starts_does_not_block_cleanup(self, monkeypatch):
        # Audit finding on #166 (round 9): the reader had started but was not
        # yet recorded, so cleanup closed its pipe itself and blocked behind
        # it while an escaped descendant held the write end.
        import threading as th
        import time

        real_start = th.Thread.start
        starts = []

        def _start_then_interrupt(self):
            real_start(self)
            starts.append(self)
            if len(starts) == 1:
                time.sleep(1.0)  # let the launcher hand the pipe to its descendant
                raise KeyboardInterrupt

        monkeypatch.setattr(th.Thread, "start", _start_then_interrupt)
        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'],"
            " start_new_session=True)\n"
            "time.sleep(60)\n"
        )
        began = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            SubprocessRunner().run(
                ExternalProcessRequest(
                    file_name=sys.executable, arguments=["-c", script], timeout_seconds=0.2
                )
            )
        assert time.monotonic() - began < 5.0


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX select-based readers")
def test_detached_worker_does_not_keep_readers_alive_after_a_timeout():
    # Audit finding on #166 (round 10): a worker that left the process group
    # kept both reader threads blocked, and their pipes open, after run()
    # returned, once per timed-out run.
    import threading as th
    import time

    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        " start_new_session=True)\n"
        "time.sleep(60)\n"
    )
    before = set(th.enumerate())
    for _ in range(2):
        result = SubprocessRunner().run(
            ExternalProcessRequest(
                file_name=sys.executable, arguments=["-c", script], timeout_seconds=0.3
            )
        )
        assert result.timed_out
    time.sleep(0.5)
    assert [t for t in th.enumerate() if t not in before] == []


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX descriptors")
def test_pipes_on_high_descriptors_are_read():
    # Audit finding on #166 (round 11): select() rejects descriptors >= 1024,
    # both readers died and the run reported success with no output.
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < 1200:
        pytest.skip("descriptor limit too low")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 1200), hard))
    held = []
    try:
        while not held or held[-1] < 1100:
            held.append(os.open(os.devnull, os.O_RDONLY))
        result = SubprocessRunner().run(
            ExternalProcessRequest(
                file_name=sys.executable,
                arguments=["-c", "print('Programming device failed')"],
                timeout_seconds=10,
            )
        )
    finally:
        for fd in held:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert result.standard_output_lines == ("Programming device failed",)


def test_a_reader_that_fails_fails_the_run(monkeypatch):
    from daqifi.firmware import external_process

    def _broken(stream, delivering):
        raise ValueError("reader exploded")
        yield  # pragma: no cover

    monkeypatch.setattr(external_process, "_iter_lines", _broken)
    with pytest.raises(OSError, match="result is unknown") as info:
        SubprocessRunner().run(
            ExternalProcessRequest(file_name=sys.executable, arguments=["-c", "print(1)"], timeout_seconds=10)
        )
    assert isinstance(info.value.__cause__, ValueError)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX non-blocking stdin")
def test_blocked_stdin_response_does_not_outlive_a_timeout():
    # Audit finding on #166 (round 11): the response write blocked on a pipe
    # an escaped descendant held but never read, leaking the reader thread.
    import threading as th
    import time

    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        " start_new_session=True)\n"
        "print('prompt', flush=True)\n"
        "time.sleep(60)\n"
    )
    before = set(th.enumerate())
    result = SubprocessRunner().run(
        ExternalProcessRequest(
            file_name=sys.executable,
            arguments=["-c", script],
            timeout_seconds=0.3,
            standard_input_response_factory=lambda line: "x" * 1_000_000,
        )
    )
    assert result.timed_out
    time.sleep(0.5)
    assert [t for t in th.enumerate() if t not in before] == []


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX descriptors")
def test_stdin_is_closed_when_its_writer_outlives_an_interrupted_cleanup(monkeypatch):
    # Audit finding on #166 (round 12): after an interrupt, cleanup left
    # stdin to the still-running stdout reader, which closed only stdout.
    import subprocess as sp
    import time

    from daqifi.firmware import external_process

    spawned = []
    real_popen = sp.Popen

    class _InterruptedPopen(real_popen):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            spawned.append(self)

        def wait(self, timeout=None):
            if timeout is not None and timeout > 5:
                time.sleep(1.0)  # let the launcher hand the pipes to its descendant
                raise KeyboardInterrupt
            return super().wait(timeout)

    monkeypatch.setattr(external_process.subprocess, "Popen", _InterruptedPopen)
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        " start_new_session=True)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(KeyboardInterrupt):
        SubprocessRunner().run(
            ExternalProcessRequest(
                file_name=sys.executable,
                arguments=["-c", script],
                timeout_seconds=10,
                standard_input_response_factory=lambda line: None,
            )
        )
    time.sleep(0.5)
    assert spawned[0].stdin.closed
