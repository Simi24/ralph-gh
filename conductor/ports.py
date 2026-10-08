"""Ports: the three seams the conductor talks through, and their data types.

Production adapters wrap `gh`, `claude` and `git`; tests use in-memory fakes
(tests/fakes.py) for Forge and Agents and the real Git adapter on a temp repo.
Extend these ports instead of bypassing them.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    labels: frozenset[str] = frozenset()
    state: str = "open"
    repo: str = ""  # owning repository, to tell cross-repo issues apart


@dataclass(frozen=True)
class Blocker:
    """An issue that blocks another one (native `blocked_by` dependency)."""

    repo: str
    number: int
    state: str  # "open" | "closed"


@dataclass(frozen=True)
class Comment:
    id: int
    body: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    head: str  # head branch name
    base: str  # base branch name
    state: str = "open"  # "open" | "merged" | "closed" (only latest_pr reports the last two)


@dataclass(frozen=True)
class SessionRequest:
    role: str  # "implementer", "ticket-gate", ...
    prompt: str
    cwd: Path
    agent: str | None = None  # --agent NAME (reviewer sessions)
    model: str | None = None  # --model NAME (writing sessions only)
    timeout: int = 7200
    add_dirs: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SessionResult:
    text: str = ""  # the session's final result text
    returncode: int = 0
    timed_out: bool = False
    usage_limit: bool = False


class Forge(Protocol):
    def get_issue(self, number: int) -> Issue: ...
    def list_sub_issues(self, prd: int) -> list[Issue]: ...
    def list_blockers(self, number: int) -> list[Blocker]:
        """Native `blocked_by` dependencies of an issue. Raise on API errors: never read as "none"."""
        ...

    def set_labels(self, number: int, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> None: ...
    def comment(self, number: int, body: str) -> None:
        """Comment on an issue or a PR."""
        ...

    def create_pr(self, *, head: str, base: str, title: str, body: str, draft: bool = False) -> PullRequest: ...
    def find_pr(self, *, head: str, base: str) -> PullRequest | None:
        """The open same-repo PR from `head` into `base`, if any."""
        ...

    def latest_pr(self, *, head: str, base: str) -> PullRequest | None:
        """The most recent PR from `head` into `base` in any state, if any. Raise on API errors."""
        ...

    def pr_head_sha(self, number: int) -> str:
        """The PR's current head commit (40-hex)."""
        ...

    def changed_files(self, number: int) -> list[str] | None:
        """Paths of the PR's whole diff; None when it cannot be read."""
        ...

    def mark_ready(self, number: int) -> None: ...
    def close_issue(self, number: int) -> None: ...
    def merge_pr(self, number: int, *, method: str, head_sha: str) -> bool:
        """Merge pinned to head_sha; True only if the merge happened."""
        ...


class CommentForge(Protocol):
    """Issue comments (#64: the status comment on the PRD). All raise on API errors."""

    def list_comments(self, number: int) -> list[Comment]: ...
    def create_comment(self, number: int, body: str) -> int:
        """Create a comment and return its id."""
        ...

    def update_comment(self, comment_id: int, body: str) -> None: ...


class Agents(Protocol):
    def start(self, request: SessionRequest) -> SessionResult: ...


class Git(Protocol):
    def create_branch(self, branch: str, base: str) -> None:
        """Ensure origin has `branch`, created from origin/`base` if missing."""
        ...

    def add_worktree(self, path: Path, branch: str, start: str) -> None:
        """Worktree at `path` on a local `branch` reset to origin/`start`."""
        ...

    def remove_worktree(self, path: Path, branch: str) -> None:
        """Remove the worktree and its local branch (the pushed branch stays)."""
        ...

    def is_behind(self, path: Path, base: str) -> bool:
        """Fetch, then True iff origin/`base` has commits the worktree's HEAD lacks."""
        ...

    def push(self, path: Path, branch: str) -> None: ...
    def head_sha(self, path: Path) -> str: ...
