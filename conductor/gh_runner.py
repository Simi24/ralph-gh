"""The one place that starts `gh`: an argv list, never a shell, stdin closed."""
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

PAGE_SIZE = 100
MAX_PAGES = 100  # a safety stop, 10 000 items

# (argv after "gh") -> completed process. Tests inject a scripted one.
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class GhError(Exception):
    pass


def subprocess_runner(cwd: Path) -> Runner:
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}

    def run(args: list[str]) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            ["gh", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace"
        )

    return run


class Gh:
    """Checked calls on top of a Runner. Every failure is a GhError."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner

    def run(self, *args: str) -> str:
        proc = self._runner(list(args))
        if proc.returncode != 0:
            raise GhError(f"gh {' '.join(args[:3])}: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def run_retry(self, *args: str) -> str:
        """For label and comment writes: one retry, then raise."""
        try:
            return self.run(*args)
        except GhError:
            return self.run(*args)

    def api(self, path: str, *fields: str, method: str = "GET") -> Any:
        out = self.run("api", "-X", method, path, *fields)
        try:
            return json.loads(out) if out.strip() else None
        except json.JSONDecodeError as error:
            raise GhError(f"gh api {path}: unreadable JSON ({error})") from None

    def api_retry(self, path: str, *fields: str, method: str = "GET") -> Any:
        try:
            return self.api(path, *fields, method=method)
        except GhError:
            return self.api(path, *fields, method=method)

    def pages(self, path: str) -> list[Any]:
        """Every element of a paginated list endpoint (`per_page=100`, page by page)."""
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            batch = self.api(f"{path}?per_page={PAGE_SIZE}&page={page}")
            if not isinstance(batch, list):
                raise GhError(f"gh api {path}: expected a JSON array")
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                return items
        raise GhError(f"gh api {path}: more than {MAX_PAGES * PAGE_SIZE} items")
