"""The real Agents adapter: one `claude --print --output-format json` process per session.

- Thread-safe: no shared mutable state, each call owns its process.
- Every process starts through the stop registry (`SessionRegistry.spawn`), so it
  lives in its own process group and an immediate stop can kill it.
- The prompt goes on stdin; the session is killed (whole group: SIGTERM, a grace
  period, SIGKILL) when `SessionRequest.timeout` expires.
- Every session loads the worker bundle (`--plugin-dir`) and runs with `RALPH_GUARD=1`
  (see `conductor/worker_bundle.py`); only exploration gets extra writable roots.
- Reviewer sessions get `--agent NAME` and no `--model` (the agent pins it);
  writing sessions get `--model` when one is set.
"""
import logging
import signal
import subprocess
from pathlib import Path

from conductor.claude_output import parse_session
from conductor.ports import SessionRequest, SessionResult
from conductor.sessions import SessionRegistry, kill_group
from conductor.worker_bundle import bundle_dir, guard_env

log = logging.getLogger("conductor")

KILL_GRACE = 10.0  # seconds between SIGTERM and SIGKILL on a timeout


def build_command(base: tuple[str, ...], request: SessionRequest, bundle: Path) -> list[str]:
    command = [*base, "--dangerously-skip-permissions", "--print", "--output-format", "json"]
    command += ["--plugin-dir", str(bundle)]
    for directory in request.add_dirs:
        command += ["--add-dir", str(directory)]
    if request.agent:
        command += ["--agent", request.agent]
    if request.model:
        command += ["--model", request.model]
    return command


class ClaudeAgents:
    def __init__(
        self,
        sessions: SessionRegistry,
        command: tuple[str, ...] = ("claude",),
        kill_grace: float = KILL_GRACE,
        *,
        bundle: Path | None = None,
        allow_manifest_edits: bool = False,
    ) -> None:
        self._sessions = sessions
        self._bundle = bundle or bundle_dir()
        self._allow_manifest_edits = allow_manifest_edits
        self._command = command
        self._kill_grace = kill_grace

    def start(self, request: SessionRequest) -> SessionResult:
        proc = self._sessions.spawn(
            build_command(self._command, request, self._bundle),
            cwd=request.cwd,
            env=guard_env(request, self._allow_manifest_edits),
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
        kill_group(proc.pid, signal.SIGTERM)
        try:
            return proc.communicate(timeout=self._kill_grace)
        except subprocess.TimeoutExpired:
            kill_group(proc.pid, signal.SIGKILL)
            return proc.communicate()
