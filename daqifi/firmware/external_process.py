"""External-process execution abstraction for firmware update workflows.

Port of `Daqifi.Core.Firmware.ExternalProcessRequest`,
`ExternalProcessResult`, `IExternalProcessRunner`, and
`ProcessExternalProcessRunner`.

The C# version uses `Process` with separate stdout/stderr capture
threads and a cancellable wait. Python's `subprocess.Popen` plus
threads on `process.stdout.readline()` gives the same shape; we use
`subprocess.run` for simple invocations and a manual Popen loop only
when callbacks are registered.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, Union, runtime_checkable


__all__ = [
    "ExternalProcessRequest",
    "ExternalProcessResult",
    "ExternalProcessRunner",
    "SubprocessRunner",
]


_DEFAULT_TIMEOUT_SECONDS = 5 * 60.0  # 5 minutes — matches C# TimeSpan.FromMinutes(5)


@dataclass
class ExternalProcessRequest:
    """Process execution request — knobs for `ExternalProcessRunner.run`.

    `arguments` accepts either a shell-style string (parsed via
    `shlex.split`) OR a pre-split sequence of strings. The list form
    matches Python's `subprocess` idiom and is preferred when an
    argument may contain spaces or quote characters.
    """

    file_name: str = ""
    arguments: Union[str, Sequence[str]] = ""
    working_directory: Optional[str] = None
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    on_standard_output_line: Optional[Callable[[str], None]] = None
    on_standard_error_line: Optional[Callable[[str], None]] = None
    standard_input_response_factory: Optional[Callable[[str], Optional[str]]] = None


@dataclass(frozen=True)
class ExternalProcessResult:
    """Result payload from an external-process run."""

    exit_code: int
    timed_out: bool
    duration_seconds: float
    # `tuple` makes the dataclass deeply immutable — `frozen=True` only
    # locks the dataclass attributes, not the values they reference.
    standard_output_lines: Tuple[str, ...] = field(default_factory=tuple)
    standard_error_lines: Tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class ExternalProcessRunner(Protocol):
    """Abstraction for launching external tools (Pythonic name for `IExternalProcessRunner`)."""

    def run(self, request: ExternalProcessRequest) -> ExternalProcessResult:
        ...


_IS_WINDOWS = sys.platform.startswith("win")


def _split_arguments(arguments: Union[str, Sequence[str]]) -> List[str]:
    """Normalize the `arguments` field into a list passed to subprocess.

    String inputs go through POSIX `shlex.split`. (On Windows a string is
    not split at all; `_build_command` passes it through verbatim.)
    Sequence inputs (list/tuple) are returned as-is — useful when an
    argument needs to contain literal spaces or quote characters.
    """
    if not arguments:
        return []
    if isinstance(arguments, str):
        return shlex.split(arguments)
    return list(arguments)


def _build_command(request: "ExternalProcessRequest") -> Union[str, List[str]]:
    if _IS_WINDOWS and isinstance(request.arguments, str):
        # A Windows command line is one string that the callee parses
        # itself, which is what upstream's ProcessStartInfo.Arguments
        # passes. Splitting it and letting list2cmdline re-quote each piece
        # turned '"C:\Firmware Files\x.bin"' into an argument with literal
        # quotes in it.
        command = subprocess.list2cmdline([request.file_name])
        if request.arguments:
            command += " " + request.arguments
        return command
    return [request.file_name] + _split_arguments(request.arguments)


class _WindowsJob:
    """A Job Object that contains the child and everything it starts.

    Windows has no process groups that outlive their leader: once a launcher
    exits, ``taskkill /T`` can no longer find the workers it left behind, and
    a flash worker would keep driving the device after a timeout. A job
    contains every descendant regardless of who exited, and terminating it
    kills them all. It is terminated only on timeout or interruption, like
    the POSIX process group; closing it leaves a finished run's descendants
    alone. Any failure leaves the job unset and the runner falls back to
    ``taskkill /T``.

    The child is assigned right after it starts, so anything it spawns in
    that first instant is outside the job. ``taskkill /T`` runs first on
    timeout and covers those while the child is still alive; a worker
    spawned in that instant by a launcher that has also exited before the
    timeout is beyond both (``subprocess`` cannot start a child suspended).
    """

    def __init__(self) -> None:
        self._handle = None
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            self._kernel32 = kernel32
            self._handle = handle
        except Exception:
            self._handle = None

    def assign(self, proc: "subprocess.Popen") -> None:
        if self._handle is None:
            return
        process_handle = getattr(proc, "_handle", None)
        if process_handle is None or not self._kernel32.AssignProcessToJobObject(
            self._handle, int(process_handle)
        ):
            self.close()

    def terminate(self) -> bool:
        if self._handle is None:
            return False
        return bool(self._kernel32.TerminateJobObject(self._handle, 1))

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _kill_process_tree(
    proc: "subprocess.Popen", job: "Optional[_WindowsJob]" = None
) -> None:
    """Kill the process and everything it started (upstream:
    ``Process.Kill(entireProcessTree: true)``).

    Killing only the direct child left its descendants running, still
    holding the output pipes, so a flash tool's real worker outlived the
    timeout and the reader threads never saw EOF.
    """
    try:
        if _IS_WINDOWS:
            # Both, in this order. taskkill /T follows parent links, so while
            # the launcher lives it reaches workers spawned before the job
            # assignment; the job then reaches workers whose launcher has
            # already exited, which taskkill /T cannot. Terminating the job
            # first would kill the launcher and cut the links taskkill needs.
            if proc.poll() is None:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            if job is not None:
                job.terminate()
        else:
            # start_new_session made the child a group leader, so its pid
            # is the group id; descendants stay in the group unless they
            # deliberately leave it.
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


class SubprocessRunner:
    """Concrete `ExternalProcessRunner` backed by Python's `subprocess` module.

    Pythonic name for `ProcessExternalProcessRunner` — `Subprocess`
    matches Python idiom and is what callers will actually search for.
    """

    def run(self, request: ExternalProcessRequest) -> ExternalProcessResult:
        if request is None:
            raise TypeError("request must not be None")
        if not request.file_name:
            raise ValueError("request.file_name must not be empty")

        cmd = _build_command(request)

        stdout_lines: List[str] = []
        stderr_lines: List[str] = []

        start = time.monotonic()
        timed_out = False

        group_kwargs: Dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
            if _IS_WINDOWS
            else {"start_new_session": True}
        )
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=request.working_directory,
                stdin=subprocess.PIPE if request.standard_input_response_factory else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                **group_kwargs,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"External process executable not found: {request.file_name!r}"
            ) from exc
        job = _WindowsJob() if _IS_WINDOWS else None
        if job is not None:
            job.assign(proc)

        stdout_thread = threading.Thread(
            target=_pump_stream,
            args=(proc.stdout, stdout_lines, request.on_standard_output_line, request.standard_input_response_factory, proc.stdin),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_pump_stream,
            args=(proc.stderr, stderr_lines, request.on_standard_error_line, None, None),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timeout = request.timeout_seconds
        deadline = None if timeout is None else start + timeout
        try:
            try:
                exit_code = proc.wait(timeout=timeout)
                # Done means the output is done too, as with .NET's
                # WaitForExit(): a descendant that inherited the pipes may
                # still be writing (a launcher that exits while its worker
                # flashes and reports). Its output decides success, so wait
                # for EOF within the same budget.
                for thread in (stdout_thread, stderr_thread):
                    thread.join(
                        None if deadline is None
                        else max(0.0, deadline - time.monotonic())
                    )
                if stdout_thread.is_alive() or stderr_thread.is_alive():
                    timed_out = True
                    _kill_process_tree(proc, job)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_tree(proc, job)
                exit_code = proc.wait()
        except BaseException:
            # KeyboardInterrupt and friends: the child is in its own
            # session/group and won't see the terminal's signal.
            _kill_process_tree(proc, job)
            raise
        finally:
            stdout_thread.join(timeout=2.0)
            stderr_thread.join(timeout=2.0)
            # The pump threads close their own streams at EOF. One still
            # alive is blocked on a pipe a descendant outside the process
            # group holds; closing that stream from here would block behind
            # it, which is how run() used to hang past its timeout. Leave it
            # to the daemon thread. stdin is written by the stdout pump, so
            # it is only safe to close once that thread is done.
            if proc.stdin is not None and not stdout_thread.is_alive():
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if job is not None:
                job.close()

        duration = time.monotonic() - start

        return ExternalProcessResult(
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=duration,
            standard_output_lines=tuple(stdout_lines),
            standard_error_lines=tuple(stderr_lines),
        )


def _pump_stream(
    stream,
    sink: List[str],
    callback: Optional[Callable[[str], None]],
    response_factory: Optional[Callable[[str], Optional[str]]],
    stdin,
) -> None:
    """Read `stream` line-by-line, capture into `sink`, and fire callbacks."""
    if stream is None:
        return
    try:
        for raw_line in stream:
            line = raw_line.rstrip("\r\n")
            sink.append(line)
            if callback is not None:
                try:
                    callback(line)
                except Exception:
                    # Callback errors must not kill the pump thread —
                    # the C# version swallows them in the same way.
                    pass
            if response_factory is not None and stdin is not None:
                response = response_factory(line)
                if response is not None:
                    try:
                        stdin.write(response + "\n")
                        stdin.flush()
                    except Exception:
                        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass
