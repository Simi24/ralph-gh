"""Find the repo the CLI was started in: its root and its `owner/name`.

Works from any directory inside the repo (a subdirectory or a worktree)."""
import subprocess
from dataclasses import dataclass
from pathlib import Path


class RepoError(Exception):
    pass


@dataclass(frozen=True)
class RepoContext:
    root: Path
    name: str  # owner/name


def _output(argv: list[str], cwd: Path, what: str) -> str:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RepoError(f"{argv[0]} not found in PATH") from None
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        raise RepoError(f"could not resolve {what}: {proc.stderr.strip() or 'no output'}")
    return out


def find_repo(cwd: Path) -> RepoContext:
    root = Path(_output(["git", "rev-parse", "--show-toplevel"], cwd, "the repository root (not inside a git repo?)"))
    name = _output(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], root, "the GitHub repo")
    return RepoContext(root, name)
