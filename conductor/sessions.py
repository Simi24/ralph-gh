"""Session process registry: every child process the conductor starts for a
session or a verify command lives in its own process group and is tracked
here, so an immediate stop can kill the whole group (critical path: signals).

Adapters must start their `claude` and verify processes through `spawn` or
`run`. After `kill_all` the registry is closed: a late `spawn` raises
`Stopped` instead of starting work nobody will wait for.
"""
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

GRACE_SECONDS = 2.0  # between SIGTERM and SIGKILL


def kill_group(pgid: int, signum: int, killpg: Callable[[int, int], None] = os.killpg) -> None:
    """Signal a whole process group; one that is already gone is not an error."""
    try:
        killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def run_detached(args: Any, **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    """`subprocess.run` with captured text output in its own session, so a terminal
    Ctrl-C (SIGINT to the foreground process group) never kills a `git` or `gh`
    call halfway through a merge or a push. Callers pass `cwd`, `timeout`, ..."""
    return subprocess.run(
        args, start_new_session=True, capture_output=True, text=True, errors="replace", **kwargs
    )


class Stopped(Exception):
    """The run is being stopped immediately: abandon this work."""


class SessionRegistry:
    def __init__(self, killpg: Callable[[int, int], None] = os.killpg, grace: float = GRACE_SECONDS) -> None:
        self._killpg = killpg
        self._grace = grace
        self._lock = threading.RLock()  # re-entrant: a signal handler may run kill_all mid-spawn
        self._procs: set[subprocess.Popen[Any]] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def spawn(self, args: Any, **kwargs: Any) -> "subprocess.Popen[Any]":
        """Popen in a new process group (pgid == pid), tracked until `release`."""
        with self._lock:
            if self._closed:
                raise Stopped("the run is stopping")
            proc = subprocess.Popen(args, start_new_session=True, **kwargs)
            self._procs.add(proc)
            return proc

    def release(self, proc: "subprocess.Popen[Any]") -> None:
        with self._lock:
            self._procs.discard(proc)

    def run(self, args: Any, **kwargs: Any) -> "subprocess.CompletedProcess[str]":
        """`subprocess.run` with captured text output, killable by `kill_all`."""
        proc = self.spawn(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", **kwargs)
        try:
            out, err = proc.communicate()
        finally:
            self.release(proc)
        return subprocess.CompletedProcess(args, proc.returncode, out, err)

    def kill_all(self) -> None:
        """SIGTERM every tracked group, wait up to the grace period, then SIGKILL every group."""
        with self._lock:
            self._closed = True
            procs = list(self._procs)
        for proc in procs:
            self._signal(proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + self._grace
        for proc in procs:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for proc in procs:  # also reaches grandchildren that outlived their leader
            self._signal(proc.pid, signal.SIGKILL)

    def _signal(self, pgid: int, signum: int) -> None:
        kill_group(pgid, signum, self._killpg)
