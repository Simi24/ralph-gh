"""Startup checks. A broken environment fails here, before any session is spawned.

`preflight(config, env)` returns None when healthy, else one message that starts
with the name of the failed check. `env` is the `Environment` seam; the real one
is `conductor/host.py`.
"""
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from conductor.config import Config

# (name, color, description). Existing labels are never touched.
REQUIRED_LABELS: tuple[tuple[str, str, str], ...] = (
    ("ralph:queued", "0E8A16", "ralph-gh: ready to work"),
    ("ralph:in-progress", "FBCA04", "ralph-gh: ticket being implemented"),
    ("ralph:in-review", "1D76DB", "ralph-gh: ticket PR open, awaiting gate"),
    ("ralph:integrated", "5319E7", "ralph-gh: merged into the integration branch"),
    ("ralph:hitl-arch", "FFA500", "ralph-gh: architecturally sensitive, never auto-merge"),
    ("ralph:gate-passed", "0052CC", "ralph-gh: review PASS, merge withheld for a human"),
    ("ralph:done", "5319E7", "ralph-gh: merged"),
    ("ralph:blocked", "B60205", "ralph-gh: a human decision is needed"),
    ("ralph:failed:systemic", "B60205", "ralph-gh: infra/tooling failure"),
    ("ralph:failed:issue", "D93F0B", "ralph-gh: per-issue implementation failure"),
)

_TOOLS = ("claude", "gh", "git")
_RALPH_UNTRACKED = {"?? .ralph-gh.toml", "?? .ralph-gh.config", "?? .ralph-gh/"}


class Environment(Protocol):
    def has_tool(self, name: str) -> bool: ...
    def gh_authenticated(self) -> bool: ...
    def permissions(self) -> dict[str, bool] | None:
        """The gh account's permissions on this repo, None if unreadable."""
        ...

    def git_status(self) -> list[str]:
        """`git status --porcelain` lines."""
        ...

    def fetch_base(self, base: str) -> bool: ...
    def existing_labels(self) -> set[str]: ...
    def create_label(self, name: str, color: str, description: str) -> None: ...
    def agent_roots(self) -> list[Path]:
        """Directories searched for agent definitions (user, then repo)."""
        ...

    def run_shell(self, command: str) -> int: ...
    def probe_health(self, url: str) -> str | None:
        """None when healthy, else a short failure reason."""
        ...

    def sleep(self, seconds: float) -> None: ...


def find_agent(name: str, roots: list[Path]) -> Path | None:
    """The .md file whose frontmatter `name:` equals `name` (not the file name)."""
    pattern = re.compile(rf"""^name:\s*["']?{re.escape(name)}["']?\s*$""")
    for root in roots:
        for directory, _, files in os.walk(root, followlinks=True):
            for file in sorted(files):
                path = Path(directory) / file
                if file.endswith(".md") and _frontmatter_names(path, pattern):
                    return path
    return None


def _frontmatter_names(path: Path, pattern: re.Pattern[str]) -> bool:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return False
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if pattern.match(line):
            return True
    return False


def _check_tools(config: Config, env: Environment) -> str | None:
    for tool in _TOOLS:
        if not env.has_tool(tool):
            return f"tool {tool}: not found in PATH"
    if not env.gh_authenticated():
        return "gh auth: gh is not authenticated (run `gh auth login`)"
    return None


def _check_permissions(config: Config, env: Environment) -> str | None:
    perms = env.permissions()
    if perms is None:
        return "permissions: could not read the gh account's permissions on this repo"
    if not perms.get("push"):
        return "permissions: missing push access (needed to push branches and merge PRs)"
    if not perms.get("triage"):
        return "permissions: missing triage access (needed to create and edit ralph:* labels)"
    return None


def _check_clean_tree(config: Config, env: Environment) -> str | None:
    dirty = [line for line in env.git_status() if line.strip() not in _RALPH_UNTRACKED]
    if dirty:
        return "clean tree: working tree not clean:\n" + "\n".join(dirty)
    return None


def _check_fetch(config: Config, env: Environment) -> str | None:
    if not env.fetch_base(config.base_branch):
        return f"fetch: could not fetch origin/{config.base_branch} (network? auth?)"
    return None


def _check_agent(label: str, name: str, env: Environment) -> str | None:
    if ":" in name:  # plugin:agent, only name-checked at config load
        return None
    if find_agent(name, env.agent_roots()) is None:
        return (
            f"{label}: no agent named '{name}' in the user or repo agents directory "
            "(run install.sh?)"
        )
    return None


def _check_reviewer_agent(config: Config, env: Environment) -> str | None:
    return _check_agent("reviewer agent", config.reviewer_agent, env)


def _check_ticket_gate_agent(config: Config, env: Environment) -> str | None:
    return _check_agent("ticket gate agent", config.ticket_gate_agent, env)


def _check_labels(config: Config, env: Environment) -> str | None:
    existing = env.existing_labels()
    for name, color, description in REQUIRED_LABELS:
        if name not in existing:
            env.create_label(name, color, description)
    return None


def _check_preflight_command(config: Config, env: Environment) -> str | None:
    if not config.preflight_command:
        return None
    if config.preflight_health_url and env.probe_health(config.preflight_health_url) is None:
        return None  # already healthy: nothing to start
    if env.run_shell(config.preflight_command) != 0:
        return f"preflight command: failed: {config.preflight_command}"
    return None


def _check_health(config: Config, env: Environment) -> str | None:
    url = config.preflight_health_url
    if not url:
        return None
    reason = "no attempt made"
    attempts = config.preflight_health_retries
    for attempt in range(attempts):
        result = env.probe_health(url)
        if result is None:
            return None
        reason = result
        if attempt < attempts - 1:
            env.sleep(1)
    return f"preflight health check: never went green: {url} (gave up after {attempts} attempts; {reason})"


_CHECKS: tuple[Callable[[Config, Environment], str | None], ...] = (
    _check_tools,
    _check_permissions,
    _check_clean_tree,
    _check_fetch,
    _check_reviewer_agent,
    _check_ticket_gate_agent,
    _check_labels,
    _check_preflight_command,
    _check_health,
)


def preflight(config: Config, env: Environment) -> str | None:
    for check in _CHECKS:
        message = check(config, env)
        if message is not None:
            return message
    return None
