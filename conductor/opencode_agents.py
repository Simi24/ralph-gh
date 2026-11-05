"""The OpenCode Agents adapter: one `opencode run --format json` process per session.

- Thread-safe: no shared mutable state, each call owns its process.
- Starts through the stop registry (`SessionRegistry.spawn`), living in its own process group.
- The prompt goes on stdin, never on argv.
- The session runs in `request.cwd` through `--dir` and `PWD`: OpenCode ignores the process cwd, so
  without them a session would work (and write) wherever the conductor was started.
- Runs with `guard_env(request, ...)` plus `OPENCODE_CONFIG_CONTENT`: the session config that loads
  the bundle's OpenCode guard plugin (`worker-bundle/opencode/ralph-guard.ts`, the same rules as the
  Claude hooks), turns sharing and auto-update off, and, for a review session, defines the bundle
  agent it runs as (read-only: edits denied). A session never starts without the guard plugin, and
  only the bundle's own agents (`ralph-guard:<name>`) can run here: anything else fails closed.
- After the session, an edited dependency manifest turns the result into RALPH:BLOCKED.
- Parses output into SessionResult using `conductor.opencode_output`.
"""
import json
import logging
import re
import signal
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from conductor.opencode_output import parse_session
from conductor.ports import SessionRequest, SessionResult
from conductor.sessions import SessionRegistry, kill_group
from conductor.worker_bundle import BUNDLE_NAME, bundle_dir, check_manifest_edits, guard_env

log = logging.getLogger("conductor")

KILL_GRACE = 10.0  # seconds between SIGTERM and SIGKILL on a timeout
GUARD_PLUGIN = Path("opencode") / "ralph-guard.ts"
_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class AgentUnavailable(Exception):
    """The session cannot run as asked; the message says why."""


def build_command(base: tuple[str, ...], request: SessionRequest, default_model: str | None) -> list[str]:
    command = [*base, "run", "--format", "json"]
    if request.cwd:  # OpenCode takes its project directory from PWD or --dir, never from the process cwd
        command += ["--dir", str(request.cwd)]
    if request.agent:
        command += ["--agent", request.agent.rpartition(":")[2]]
    model = request.model or default_model
    if model:
        command += ["--model", model]
    return command


def _agent_prompt(definition: Path) -> str:
    """The body of a Claude agent definition, without its frontmatter."""
    text = definition.read_text()
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5 :]
    return text.lstrip("\n")


def session_config(request: SessionRequest, bundle: Path) -> dict[str, Any]:
    guard = bundle / GUARD_PLUGIN
    if not guard.is_file():
        raise AgentUnavailable(f"ralph-guard: the OpenCode guard plugin is missing ({guard}); sessions never start unguarded")
    config: dict[str, Any] = {
        "plugin": [guard.as_uri()],
        "share": "disabled",
        "autoupdate": False,
        "permission": {"external_directory": "allow"},  # writes outside the worktree are the guard's call
    }
    if request.agent:
        plugin, _, name = request.agent.rpartition(":")
        definition = bundle / "agents" / f"{name}.md"
        if plugin != BUNDLE_NAME or not _AGENT_NAME.match(name) or not definition.is_file():
            raise AgentUnavailable(
                f"the opencode engine only runs the worker bundle's agents ({BUNDLE_NAME}:<name>), not '{request.agent}'"
            )
        config["agent"] = {name: {"mode": "primary", "prompt": _agent_prompt(definition), "permission": {"edit": "deny"}}}
    return config


class OpencodeAgents:
    def __init__(
        self,
        sessions: SessionRegistry,
        command: tuple[str, ...] = ("opencode",),
        kill_grace: float = KILL_GRACE,
        default_model: str | None = None,
        *,
        bundle: Path | None = None,
        allow_manifest_edits: bool = False,
    ) -> None:
        self._sessions = sessions
        self._command = command
        self._kill_grace = kill_grace
        self._default_model = default_model
        self._bundle = bundle or bundle_dir()
        self._allow_manifest_edits = allow_manifest_edits

    def start(self, request: SessionRequest) -> SessionResult:
        try:
            config = session_config(request, self._bundle)
        except AgentUnavailable as e:
            log.warning(f"[guard] opencode session '{request.role}' not started: {e}")
            return SessionResult(text=f"ralph-gh: {e}", returncode=1)
        env = guard_env(request, self._allow_manifest_edits)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        if request.cwd:
            env["PWD"] = str(request.cwd)
        proc = self._sessions.spawn(
            build_command(self._command, request, self._default_model),
            cwd=request.cwd,
            env=env,
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
                log.warning(f"[timeout] opencode session '{request.role}' exceeded {request.timeout}s and was killed")
                stdout, stderr = self._kill(proc)
                return parse_session(stdout, stderr, proc.returncode, timed_out=True)
            result = parse_session(stdout, stderr, proc.returncode)
        finally:
            self._sessions.release(proc)
        if not self._allow_manifest_edits and request.cwd:
            manifest = check_manifest_edits(request.cwd)
            if manifest:
                log.warning(f"[guard] opencode session '{request.role}' edited manifest: {manifest}")
                text = f"ralph-guard: dependency manifests are not edited by sessions ({manifest}); report RALPH:BLOCKED if the ticket needs it\nRALPH:BLOCKED"
                return replace(result, text=text)
        return result

    def _kill(self, proc: "subprocess.Popen[str]") -> tuple[str, str]:
        """SIGTERM the process group, wait the grace period, SIGKILL it, reap."""
        kill_group(proc.pid, signal.SIGTERM)
        try:
            return proc.communicate(timeout=self._kill_grace)
        except subprocess.TimeoutExpired:
            kill_group(proc.pid, signal.SIGKILL)
            return proc.communicate()
