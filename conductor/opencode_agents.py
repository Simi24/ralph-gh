"""The OpenCode Agents adapter: one `opencode run --format json` process per session.

- Thread-safe: no shared mutable state, each call owns its process.
- Starts through the stop registry (`SessionRegistry.spawn`), living in its own process group.
- The prompt goes on stdin, never on argv.
- The session runs in `request.cwd` through `--dir` and `PWD`: OpenCode ignores the process cwd, so
  without them a session would work (and write) wherever the conductor was started.
- Runs with `guard_env(request, ...)` minus every inherited `OPENCODE_*` switch, with project config
  off (a worker could write repo-level plugins, agents or permissions), plus `OPENCODE_CONFIG_CONTENT`:
  the session config that loads the bundle's OpenCode guard plugin (`worker-bundle/opencode/ralph-guard.ts`,
  the same rules as the Claude hooks), loads the repo's AGENTS.md (or CLAUDE.md), turns sharing and
  auto-update off, and, for a review session, defines the bundle agent it runs as (no edits, no subagents).
- Fail closed: no guard plugin file, or an agent outside the bundle (`ralph-guard:<name>`), means no
  session; a session whose plugin did not report loading (OpenCode skips a broken plugin silently),
  or a review that fell back to another agent, is discarded.
- After the session, an edited dependency manifest turns the result into RALPH:BLOCKED.
- Parses output into SessionResult using `conductor.opencode_output`.
"""
import json
import logging
import re
import secrets
import signal
import subprocess
import tempfile
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
_FALLBACK = re.compile(r"[Ff]alling back to (?:the )?default agent|agent \"[^\"]*\" not found")


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


def _repo_rules(cwd: Path | None) -> Path | None:
    """The repo's agent instructions, as OpenCode itself picks them: AGENTS.md, else CLAUDE.md."""
    if cwd is None:
        return None
    return next((path for path in (cwd / "AGENTS.md", cwd / "CLAUDE.md") if path.is_file()), None)


def session_env(request: SessionRequest, allow_manifest_edits: bool, config: dict[str, Any], ready: Path, nonce: str) -> dict[str, str]:
    """guard_env without any inherited OPENCODE_* switch (OPENCODE_PURE alone would drop the guard plugin)."""
    env = {k: v for k, v in guard_env(request, allow_manifest_edits).items() if not k.startswith("OPENCODE_")}
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"  # no repo-level plugins, agents or permissions: a worker could write them
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env["RALPH_GUARD_OPENCODE_READY"] = str(ready)
    env["RALPH_GUARD_OPENCODE_NONCE"] = nonce
    if request.cwd:
        env["PWD"] = str(request.cwd)
    return env


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
    rules = _repo_rules(request.cwd)
    if rules:  # project config is off (OPENCODE_DISABLE_PROJECT_CONFIG), which also stops OpenCode reading them
        config["instructions"] = [str(rules)]
    if request.agent:
        plugin, _, name = request.agent.rpartition(":")
        definition = bundle / "agents" / f"{name}.md"
        if plugin != BUNDLE_NAME or not _AGENT_NAME.match(name) or not definition.is_file():
            raise AgentUnavailable(
                f"the opencode engine only runs the worker bundle's agents ({BUNDLE_NAME}:<name>), not '{request.agent}'"
            )
        config["agent"] = {
            name: {"mode": "primary", "disable": False, "prompt": _agent_prompt(definition), "permission": {"edit": "deny", "task": "deny"}}
        }
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
        with tempfile.TemporaryDirectory(prefix="ralph-opencode-") as handshake:
            ready = Path(handshake) / "ready"
            nonce = secrets.token_hex(16)
            env = session_env(request, self._allow_manifest_edits, config, ready, nonce)
            result, stderr = self._run(request, env)
            loaded = ready.is_file() and ready.read_text() == nonce
        if result.timed_out:
            return result
        refusal = self._refusal(request, loaded, stderr)
        if refusal:
            log.warning(f"[guard] opencode session '{request.role}' discarded: {refusal}")
            return SessionResult(text=f"ralph-guard: {refusal}", returncode=1)
        if not self._allow_manifest_edits and request.cwd:
            manifest = check_manifest_edits(request.cwd)
            if manifest:
                log.warning(f"[guard] opencode session '{request.role}' edited manifest: {manifest}")
                text = f"ralph-guard: dependency manifests are not edited by sessions ({manifest}); report RALPH:BLOCKED if the ticket needs it\nRALPH:BLOCKED"
                return replace(result, text=text)
        return result

    @staticmethod
    def _refusal(request: SessionRequest, loaded: bool, stderr: str) -> str | None:
        """Why a finished session's result cannot be used, or None."""
        if not loaded:
            return "the OpenCode guard plugin did not load, so the session ran unguarded and its result is discarded"
        if request.agent and _FALLBACK.search(stderr):
            return f"OpenCode fell back to its default agent instead of '{request.agent}', so the result is discarded"
        return None

    def _run(self, request: SessionRequest, env: dict[str, str]) -> tuple[SessionResult, str]:
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
                return parse_session(stdout, stderr, proc.returncode, timed_out=True), stderr
            return parse_session(stdout, stderr, proc.returncode), stderr
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
