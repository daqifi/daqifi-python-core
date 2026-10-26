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

import codecs
import os
import re
import selectors
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
    kills them all. The child is created suspended and assigned before any
    of its code runs, so nothing it spawns can start outside the job. The
    job is terminated only on timeout or interruption, like the POSIX
    process group; closing it leaves a finished run's descendants alone.
    If a job can't be made, the child starts normally and the runner falls
    back to ``taskkill /T``.
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

    @property
    def available(self) -> bool:
        return self._handle is not None

    def assign(self, proc: "subprocess.Popen") -> bool:
        if self._handle is None:
            return False
        process_handle = getattr(proc, "_handle", None)
        if process_handle is None or not self._kernel32.AssignProcessToJobObject(
            self._handle, int(process_handle)
        ):
            self.close()
            return False
        return True

    def terminate(self) -> bool:
        if self._handle is None:
            return False
        return bool(self._kernel32.TerminateJobObject(self._handle, 1))

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


_CREATE_SUSPENDED = 0x00000004


def _resume_process(pid: int) -> bool:
    """Resume every thread of a process created with CREATE_SUSPENDED.

    ``subprocess`` closes the primary thread handle it gets back from
    CreateProcess, so the threads are found through a Toolhelp snapshot.
    """
    import ctypes
    from ctypes import wintypes

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    TH32CS_SNAPTHREAD = 0x00000004
    THREAD_SUSPEND_RESUME = 0x0002
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return False
    resumed = 0
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    if kernel32.ResumeThread(thread) != 0xFFFFFFFF:
                        resumed += 1
                    kernel32.CloseHandle(thread)
            more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return resumed > 0


def _kill_process_tree(
    proc: "subprocess.Popen", job: "Optional[_WindowsJob]" = None
) -> None:
    """Kill the process and everything it started (upstream:
    ``Process.Kill(entireProcessTree: true)``).

    Killing only the direct child left its descendants running, still
    holding the output pipes, so a flash tool's real worker outlived the
    timeout and the reader threads never saw EOF.
    """
    # Each step is attempted on its own: one that fails (taskkill blocked by
    # policy, say) must not skip the others.
    if _IS_WINDOWS:
        # The job holds the child from its first instruction (it is started
        # suspended and assigned before it runs), so it reaches every
        # descendant, including workers whose launcher has already exited.
        # taskkill /T covers the fallback where no job could be made.
        if job is not None:
            job.terminate()
        if proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            # start_new_session made the child a group leader, so its pid
            # is the group id; descendants stay in the group unless they
            # deliberately leave it.
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
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

        job = _WindowsJob() if _IS_WINDOWS else None
        if _IS_WINDOWS:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            if job is not None and job.available:
                # Held until it is in the job; see _WindowsJob.
                flags |= _CREATE_SUSPENDED
            group_kwargs: Dict[str, Any] = {"creationflags": flags}
        else:
            group_kwargs = {"start_new_session": True}
        # Set up before the child exists, so nothing between Popen returning
        # and the cleanup scope can be interrupted. Popen itself is inside
        # that scope; the only window left is the single bytecode that binds
        # its result to `proc`, which Python code cannot close.
        delivering = threading.Event()
        delivering.set()
        threads: List[threading.Thread] = []
        readers: List[Tuple[threading.Thread, Any]] = []
        reader_failures: List[BaseException] = []
        timeout = request.timeout_seconds
        deadline = None if timeout is None else start + timeout
        exit_code: Optional[int] = None
        proc: Optional[subprocess.Popen] = None
        try:
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

            if job is not None and job.available:
                job.assign(proc)
                # Resumed whether or not the assignment took: a child left
                # suspended would only ever time out. Without the job, the
                # kill path falls back to taskkill /T.
                if not _resume_process(proc.pid):
                    raise OSError(
                        f"Could not resume {request.file_name!r} after starting "
                        "it suspended; it was terminated."
                    )

            stdout_thread = threading.Thread(
                target=_pump_stream,
                args=(proc.stdout, stdout_lines, request.on_standard_output_line, request.standard_input_response_factory, proc.stdin, delivering, reader_failures),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_pump_stream,
                args=(proc.stderr, stderr_lines, request.on_standard_error_line, None, None, delivering, reader_failures),
                daemon=True,
            )
            readers = [(stdout_thread, proc.stdout), (stderr_thread, proc.stderr)]
            for thread, _ in readers:
                thread.start()
                threads.append(thread)

            try:
                exit_code = proc.wait(timeout=timeout)
                # Done means the output is done too, as with .NET's
                # WaitForExit(): a descendant that inherited the pipes may
                # still be writing (a launcher that exits while its worker
                # flashes and reports). Its output decides success, so wait
                # for EOF within the same budget.
                for thread in threads:
                    thread.join(
                        None if deadline is None
                        else max(0.0, deadline - time.monotonic())
                    )
                if any(thread.is_alive() for thread in threads):
                    timed_out = True
                    _kill_process_tree(proc, job)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_tree(proc, job)
                exit_code = proc.wait()
        except BaseException:
            # Interrupted, or setup failed: kill the tree and reap the child,
            # or a caller holding on to the exception keeps a zombie.
            if proc is not None:
                _kill_process_tree(proc, job)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            raise
        finally:
            for thread, _ in readers:
                if thread.ident is not None:
                    thread.join(timeout=2.0)
            # Whatever a surviving reader sees from here on belongs to a run
            # that has already returned: it is discarded, not appended to
            # the result or handed to the caller's callbacks.
            delivering.clear()
            stdout_alive = bool(readers) and readers[0][0].is_alive()
            # The pump threads close their own streams at EOF. One still
            # alive is blocked on a pipe a descendant outside the process
            # group holds; closing that stream from here would block behind
            # it, which is how run() used to hang past its timeout. Leave it
            # to the daemon thread. stdin is written by the stdout pump, so
            # it is only safe to close once that thread is done (or never
            # started).
            if proc is not None:
                if proc.stdin is not None and not stdout_alive:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                # A pipe whose reader never started has nobody to close it.
                # Judged by the thread itself (ident is set by start()), not
                # by bookkeeping an interrupt could cut short: closing a pipe
                # that a live reader is blocked on would block here too.
                reader_of = {id(stream): thread for thread, stream in readers}
                for stream in (proc.stdout, proc.stderr):
                    thread = reader_of.get(id(stream))
                    if stream is not None and (thread is None or thread.ident is None):
                        try:
                            stream.close()
                        except OSError:
                            pass
            if job is not None:
                job.close()

        if reader_failures:
            raise OSError(
                f"Reading the output of {request.file_name!r} failed; its "
                "result is unknown."
            ) from reader_failures[0]

        duration = time.monotonic() - start

        return ExternalProcessResult(
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=duration,
            standard_output_lines=tuple(stdout_lines),
            standard_error_lines=tuple(stderr_lines),
        )


_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _iter_lines(stream, delivering: threading.Event):
    """Lines of `stream` (universal newlines), ending early once the run has
    returned (`delivering` cleared).

    On POSIX the pipe is polled (selectors: epoll/kqueue/poll, which unlike
    select() have no 1024-descriptor ceiling) so the reader can stop even
    while a descendant that left the process group keeps the write end open;
    a plain blocking read would park the thread, and its pipe, for as long
    as that descendant lives, once per timed-out run. On Windows such a
    descendant is inside the job and is killed with it, so EOF arrives and
    the text stream is read directly.
    """
    if _IS_WINDOWS:
        yield from stream
        return
    fd = stream.fileno()
    decoder = codecs.getincrementaldecoder(stream.encoding or "utf-8")(
        errors=stream.errors or "strict"
    )
    pending = ""
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while delivering.is_set():
            if not selector.select(0.1):
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                pending += decoder.decode(b"", final=True)
                if pending:
                    yield pending
                return
            pending += decoder.decode(chunk)
            # A trailing "\r" may be the first half of "\r\n"; hold it back.
            hold = pending.endswith("\r")
            if hold:
                pending = pending[:-1]
            parts = _LINE_BREAK.split(pending)
            pending = parts.pop()
            for part in parts:
                yield part + "\n"
            if hold:
                pending += "\r"


def _pump_stream(
    stream,
    sink: List[str],
    callback: Optional[Callable[[str], None]],
    response_factory: Optional[Callable[[str], Optional[str]]],
    stdin,
    delivering: threading.Event,
    failures: List[BaseException],
) -> None:
    """Read `stream` line-by-line, capture into `sink`, and fire callbacks.

    Lines read after `delivering` is cleared (the run has returned) are
    drained and dropped.
    """
    if stream is None:
        return
    try:
        for raw_line in _iter_lines(stream, delivering):
            if not delivering.is_set():
                continue
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
                        _write_response(stdin, response + "\n", delivering)
                    except Exception:
                        pass
    except BaseException as exc:
        # Reported to run(), which fails the run: a reader that died has not
        # seen the output, and the output decides whether a flash succeeded.
        failures.append(exc)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _write_response(stdin, text: str, delivering: threading.Event) -> None:
    """Write a stdin response without blocking past the end of the run.

    A descendant that holds stdin but never reads it fills the pipe; a
    blocking write would then park this reader thread (and its pipes) for
    that descendant's lifetime. On POSIX the write is non-blocking and
    abandoned once the run has returned. On Windows such a descendant is in
    the job and killed with it, which breaks the pipe.
    """
    if _IS_WINDOWS:
        stdin.write(text)
        stdin.flush()
        return
    stdin.flush()
    data = text.encode(stdin.encoding or "utf-8", stdin.errors or "strict")
    fd = stdin.fileno()
    os.set_blocking(fd, False)
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while data and delivering.is_set():
            if not selector.select(0.1):
                continue
            try:
                written = os.write(fd, data)
            except BlockingIOError:
                continue
            data = data[written:]
