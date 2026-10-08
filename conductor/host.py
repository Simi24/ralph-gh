"""The real `Environment` for preflight: argv-list subprocess calls, stdlib http.

Only `run_shell` goes through a shell, and only for the operator-authored
`preflight_command`.
"""
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from conductor.sessions import run_detached

HEALTH_TIMEOUT = 5  # seconds per attempt


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        return None  # a 3xx answers the probe; never follow it


class HostEnvironment:
    def __init__(self, repo_root: Path, log_file: Path | None = None) -> None:
        self.repo_root = repo_root
        self.log_file = log_file

    def _run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return run_detached(list(argv), cwd=self.repo_root)  # own session: Ctrl-C-proof

    def has_tool(self, name: str) -> bool:
        return shutil.which(name) is not None

    def gh_authenticated(self) -> bool:
        return self._run("gh", "auth", "status").returncode == 0

    def permissions(self) -> dict[str, bool] | None:
        repo = self._run("gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        name = repo.stdout.strip()
        if repo.returncode != 0 or not name:
            return None
        proc = self._run("gh", "api", f"repos/{name}", "--jq", ".permissions // {}")
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        return {k: v is True for k, v in data.items()} if isinstance(data, dict) else None

    def git_status(self) -> list[str]:
        proc = self._run("git", "status", "--porcelain")
        if proc.returncode != 0:
            return [f"?? (git status failed: {proc.stderr.strip()})"]  # fail closed
        return proc.stdout.splitlines()

    def fetch_base(self, base: str) -> bool:
        return self._run("git", "fetch", "--", "origin", base).returncode == 0

    def existing_labels(self) -> set[str] | None:
        proc = self._run("gh", "label", "list", "--limit", "1000", "--json", "name", "--jq", ".[].name")
        return set(proc.stdout.split("\n")) if proc.returncode == 0 else None  # unreadable is not "none"

    def create_label(self, name: str, color: str, description: str) -> bool:
        proc = self._run("gh", "label", "create", name, "--color", color, "--description", description)
        return proc.returncode == 0 or "already exists" in proc.stderr  # beyond the listing limit

    def agent_roots(self) -> list[Path]:
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return [config_dir / "agents", self.repo_root / ".claude" / "agents"]

    def run_shell(self, command: str) -> int:
        log = self.log_file.open("ab") if self.log_file else None
        try:
            return subprocess.run(command, shell=True, cwd=self.repo_root, stdout=log, stderr=log).returncode
        finally:
            if log:
                log.close()

    def probe_health(self, url: str) -> str | None:
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(url, timeout=HEALTH_TIMEOUT):
                return None
        except urllib.error.HTTPError as e:
            e.close()
            if e.code < 400:
                return None
            return "the endpoint never returned a successful response"
        except (socket.timeout, TimeoutError):
            return f"each attempt timed out with no response within the {HEALTH_TIMEOUT}s cap"
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                return f"each attempt timed out with no response within the {HEALTH_TIMEOUT}s cap"
            return "the endpoint never returned a successful response"
        except ValueError:
            return "the health URL is not a valid URL"

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
