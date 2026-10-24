"""The Antigravity Agents adapter: one `agy --dangerously-skip-permissions --output-format json` process per session.

- Thread-safe: no shared mutable state, each call owns its process.
- Starts through the stop registry (`SessionRegistry.spawn`), living in its own process group.
- Pass prompt as `--print=<prompt>` flag.
- Parses output into SessionResult using `conductor.antigravity_output`.
"""
import logging
import signal
import subprocess
from pathlib import Path

from conductor.antigravity_output import parse_session
from conductor.ports import SessionRequest, SessionResult
from conductor.sessions import SessionRegistry, kill_group

log = logging.getLogger("conductor")

KILL_GRACE = 10.0  # seconds between SIGTERM and SIGKILL on a timeout


def build_command(
    base: tuple[str, ...],
    request: SessionRequest,
    default_model: str | None = None,
) -> list[str]:
    command = [*base, "--dangerously-skip-permissions", "--output-format", "json"]
    for directory in request.add_dirs:
        command += ["--add-dir", str(directory)]
    if request.agent:
        command += ["--agent", request.agent]
    model = request.model or default_model
    if model:
        command += ["--model", model]
    command += [f"--print={request.prompt}"]
    return command


class AntigravityAgents:
    def __init__(
        self,
        sessions: SessionRegistry,
        command: tuple[str, ...] = ("agy",),
        kill_grace: float = KILL_GRACE,
        default_model: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._command = command
        self._kill_grace = kill_grace
        self._default_model = default_model

    def start(self, request: SessionRequest) -> SessionResult:
        cmd = build_command(self._command, request, self._default_model)
        proc = self._sessions.spawn(
            cmd,
            cwd=request.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        try:
            try:
                stdout, stderr = proc.communicate(timeout=request.timeout)
            except subprocess.TimeoutExpired:
                log.warning(f"[timeout] antigravity session '{request.role}' exceeded {request.timeout}s and was killed")
                stdout, stderr = self._kill(proc)
                return parse_session(stdout, stderr, proc.returncode, timed_out=True)
            return parse_session(stdout, stderr, proc.returncode)
        finally:
            self._sessions.release(proc)

    def _kill(self, proc: "subprocess.Popen[str]") -> tuple[str, str]:
        """SIGTERM the process group, wait the grace period, SIGKILL it, reap."""
        kill_group(proc.pid, signal.SIGTERM)
        try:
            return proc.communicate(timeout=self._kill_grace)
        except subprocess.TimeoutExpired:
            kill_group(proc.pid, signal.SIGKILL)
            return proc.communicate()
