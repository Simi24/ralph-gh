"""The real Agents adapter: one `claude --print --output-format json` process per session.

- Thread-safe: no shared mutable state, each call owns its process.
- Every process starts through the stop registry (`SessionRegistry.spawn`), so it
  lives in its own process group and an immediate stop can kill it.
- The prompt goes on stdin; the session is killed (whole group: SIGTERM, a grace
  period, SIGKILL) when `SessionRequest.timeout` expires.
- Reviewer sessions get `--agent NAME` and no `--model` (the agent pins it);
  writing sessions get `--model` when one is set.
"""
import logging
import os
import signal
import subprocess

from conductor.claude_output import parse_session
from conductor.ports import SessionRequest, SessionResult
from conductor.sessions import SessionRegistry

log = logging.getLogger("conductor")

KILL_GRACE = 10.0  # seconds between SIGTERM and SIGKILL on a timeout


def build_command(base: tuple[str, ...], request: SessionRequest) -> list[str]:
    command = [*base, "--dangerously-skip-permissions", "--print", "--output-format", "json"]
    for directory in request.add_dirs:
        command += ["--add-dir", str(directory)]
    if request.agent:
        command += ["--agent", request.agent]
    if request.model:
        command += ["--model", request.model]
    return command


class ClaudeAgents:
    def __init__(
        self, sessions: SessionRegistry, command: tuple[str, ...] = ("claude",), kill_grace: float = KILL_GRACE
    ) -> None:
        self._sessions = sessions
        self._command = command
        self._kill_grace = kill_grace

    def start(self, request: SessionRequest) -> SessionResult:
        proc = self._sessions.spawn(
            build_command(self._command, request),
            cwd=request.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        try:
            try:
                stdout, stderr = proc.communicate(input=request.prompt, timeout=request.timeout)
            except subprocess.TimeoutExpired:
                log.warning(f"[timeout] claude session '{request.role}' exceeded {request.timeout}s and was killed")
                stdout, stderr = self._kill(proc)
                return parse_session(stdout, stderr, proc.returncode, timed_out=True)
            return parse_session(stdout, stderr, proc.returncode)
        finally:
            self._sessions.release(proc)

    def _kill(self, proc: "subprocess.Popen[str]") -> tuple[str, str]:
        """SIGTERM the process group, wait the grace period, SIGKILL it, reap."""
        self._signal(proc.pid, signal.SIGTERM)
        try:
            return proc.communicate(timeout=self._kill_grace)
        except subprocess.TimeoutExpired:
            self._signal(proc.pid, signal.SIGKILL)
            return proc.communicate()

    @staticmethod
    def _signal(pgid: int, signum: int) -> None:
        try:
            os.killpg(pgid, signum)
        except (ProcessLookupError, PermissionError):
            pass  # already gone
