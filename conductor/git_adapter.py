"""Real Git adapter: argv-list subprocess calls against the repo checkout.

The checkout itself is never modified: branches are created on origin and
worktrees are added elsewhere.
"""
import re
import subprocess
from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitError(Exception):
    pass


class GitCli:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(["git", *args], cwd=cwd or self.repo_root, capture_output=True, text=True)
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def create_branch(self, branch: str, base: str) -> None:
        self._git("fetch", "origin")
        if self._git("ls-remote", "--heads", "origin", branch):
            return  # resume: reuse the existing integration branch
        self._git("push", "origin", f"refs/remotes/origin/{base}:refs/heads/{branch}")

    def add_worktree(self, path: Path, branch: str, start: str) -> None:
        self._git("fetch", "origin")
        self._git("worktree", "add", "-B", branch, str(path), f"origin/{start}")

    def remove_worktree(self, path: Path, branch: str) -> None:
        self._git("worktree", "remove", "--force", str(path))
        self._git("branch", "-D", branch)

    def push(self, path: Path, branch: str) -> None:
        self._git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=path)

    def head_sha(self, path: Path) -> str:
        sha = self._git("rev-parse", "HEAD", cwd=path)
        if not _SHA.match(sha):
            raise GitError(f"unexpected sha: {sha!r}")
        return sha

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Fail closed: bad shas, a failed fetch or a failed check all mean False."""
        if not (_SHA.match(ancestor) and _SHA.match(descendant)):
            return False
        try:
            self._git("fetch", "origin", descendant)  # by sha: brings its ancestry along
            self._git("merge-base", "--is-ancestor", ancestor, descendant)
        except GitError:
            return False
        return True
