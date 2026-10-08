"""Find the repo the CLI was started in: its root and its `owner/name`.

Works from any directory inside the repo (a subdirectory or a worktree).
Both lookups go through the same adapters as the run itself (`git_adapter`,
`gh_runner`), so they share its process-group handling."""
from dataclasses import dataclass
from pathlib import Path

from conductor.gh_runner import Gh, GhError, subprocess_runner
from conductor.git_adapter import GitError, git_output


class RepoError(Exception):
    pass


@dataclass(frozen=True)
class RepoContext:
    root: Path
    name: str  # owner/name


def find_repo(cwd: Path) -> RepoContext:
    try:
        root = git_output("rev-parse", "--show-toplevel", cwd=cwd)
    except GitError as error:
        raise RepoError(f"could not resolve the repository root (not inside a git repo?): {error}") from None
    except FileNotFoundError:
        raise RepoError("git not found in PATH") from None
    if not root:
        raise RepoError("could not resolve the repository root: no output")
    try:
        name = Gh(subprocess_runner(Path(root))).run("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    except GhError as error:
        raise RepoError(f"could not resolve the GitHub repo: {error}") from None
    except FileNotFoundError:
        raise RepoError("gh not found in PATH") from None
    if not name.strip():
        raise RepoError("could not resolve the GitHub repo: no output")
    return RepoContext(Path(root), name.strip())
