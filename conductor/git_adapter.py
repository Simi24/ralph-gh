"""Real Git adapter: argv-list subprocess calls against the repo checkout.

The checkout itself is never modified: branches are created on origin and
worktrees are added elsewhere. Every call runs in its own session (Ctrl-C
must not kill a push halfway), and refs go after `--` or in a full refspec.
"""
import shutil
from pathlib import Path

from conductor.ports import SHA, InfraError
from conductor.sessions import run_detached


class GitError(InfraError):
    pass


def git_output(*args: str, cwd: Path, timeout: float | None = None) -> str:
    """Stdout of `git <args>` in `cwd`; a non-zero exit raises GitError. Every `git` call
    of the conductor goes through here (or `GitCli`), never through a bare `subprocess`."""
    proc = run_detached(["git", *args], cwd=cwd, timeout=timeout)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


class GitCli:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        return git_output(*args, cwd=cwd or self.repo_root)

    def create_branch(self, branch: str, base: str) -> None:
        self._git("fetch", "origin")
        if self._git("ls-remote", "--heads", "origin", "--", branch):
            return  # resume: reuse the existing integration branch
        self._git("push", "origin", f"refs/remotes/origin/{base}:refs/heads/{branch}")

    def add_worktree(self, path: Path, branch: str, start: str) -> None:
        self._git("fetch", "origin")
        self._git("worktree", "add", "-B", branch, "--", str(path), f"origin/{start}")

    def remove_worktree(self, path: Path, branch: str) -> None:
        self._git("worktree", "remove", "--force", "--", str(path))
        self._git("branch", "-D", "--", branch)

    def is_behind(self, path: Path, base: str) -> bool:
        self._git("fetch", "origin", cwd=path)
        proc = run_detached(["git", "merge-base", "--is-ancestor", f"origin/{base}", "HEAD"], cwd=path)
        if proc.returncode == 0:
            return False
        if proc.returncode == 1:
            return True
        raise GitError(f"git merge-base: {proc.stderr.strip()}")

    def push(self, path: Path, branch: str) -> None:
        self._git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=path)  # never forced

    def delete_remote_branch(self, branch: str) -> None:
        self._git("fetch", "origin")
        if self._git("ls-remote", "--heads", "origin", "--", branch):  # already gone is fine
            self._git("push", "origin", f":refs/heads/{branch}")

    def head_sha(self, path: Path) -> str:
        sha = self._git("rev-parse", "HEAD", cwd=path)
        if not SHA.match(sha):
            raise GitError(f"unexpected sha: {sha!r}")
        return sha

    def remote_sha(self, branch: str) -> str:
        self._git("fetch", "origin")
        sha = self._git("rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}")
        if not SHA.match(sha):
            raise GitError(f"unexpected sha: {sha!r}")
        return sha

    def add_detached_worktree(self, path: Path, sha: str) -> None:
        if not SHA.match(sha):
            raise GitError(f"not a full commit sha: {sha!r}")
        self._git("fetch", "origin")
        self._git("worktree", "add", "--detach", "--", str(path), sha)

    def remove_detached_worktree(self, path: Path) -> None:
        self._git("worktree", "remove", "--force", "--", str(path))

    def remove_stale_worktrees(self, root: Path) -> None:
        base = root.resolve()
        errors: list[str] = []
        branches: list[str] = []
        for path, branch in self._worktrees():
            if path.parent != base:
                continue  # only the direct children of `root`: never the checkout or anything else
            if branch:
                branches.append(branch)
            self._attempt(errors, "worktree", "remove", "--force", "--", str(path))
        self._attempt(errors, "worktree", "prune")
        for branch in branches:
            self._attempt(errors, "branch", "-D", "--", branch)
        if base.is_dir():
            for left in base.iterdir():  # a directory git no longer lists would still block the next add
                if left.is_dir():
                    shutil.rmtree(left, ignore_errors=True)
                else:
                    left.unlink(missing_ok=True)
        if errors:
            raise GitError("; ".join(errors))

    def _worktrees(self) -> list[tuple[Path, str | None]]:
        """(path, local branch or None) of every worktree git lists, from the porcelain output."""
        found: list[tuple[Path, str | None]] = []
        for block in self._git("worktree", "list", "--porcelain").split("\n\n"):
            path: Path | None = None
            branch: str | None = None
            for line in block.splitlines():
                if line.startswith("worktree "):
                    path = Path(line.removeprefix("worktree ")).resolve()
                elif line.startswith("branch refs/heads/"):
                    branch = line.removeprefix("branch refs/heads/")
            if path is not None:
                found.append((path, branch))
        return found

    def _attempt(self, errors: list[str], *args: str) -> None:
        try:
            self._git(*args)
        except GitError as error:
            errors.append(str(error))

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Fail closed: bad shas, a failed fetch or a failed check all mean False."""
        if not (SHA.match(ancestor) and SHA.match(descendant)):
            return False
        try:
            self._git("fetch", "origin", descendant)  # by sha: brings its ancestry along
            self._git("merge-base", "--is-ancestor", ancestor, descendant)
        except GitError:
            return False
        return True
