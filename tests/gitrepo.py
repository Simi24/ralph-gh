"""Test fixture: a real temporary git repo with a local bare remote.

    repo = TempRepo()          # in setUp
    self.addCleanup(repo.cleanup)
    repo.checkout              # clone on `main` with one commit, pushed to origin
    repo.origin                # the bare remote
    repo.git("log", cwd=...)   # run git, return stdout
"""
import subprocess
import tempfile
from pathlib import Path


class TempRepo:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.root = root
        self.origin = root / "origin.git"
        self.checkout = root / "checkout"
        self.state_root = root / "state"
        self.git("init", "--bare", "--initial-branch=main", str(self.origin), cwd=root)
        self.git("clone", str(self.origin), str(self.checkout), cwd=root)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
            self.git("config", key, value)
        (self.checkout / "README.md").write_text("hello\n")
        self.git("add", "-A")
        self.git("commit", "-m", "initial")
        self.git("push", "origin", "HEAD:refs/heads/main")
        self.git("fetch", "origin")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(["git", *args], cwd=cwd or self.checkout, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {proc.stderr}")
        return proc.stdout.strip()

    def merge_branch(self, head: str, base: str) -> None:
        """What GitHub's merge button does: a real merge commit of `head` onto `base`, on origin.
        Plumbing in the bare repo, so no checkout is disturbed. Raises on a conflict."""
        into = self.git("rev-parse", f"refs/heads/{base}", cwd=self.origin)
        other = self.git("rev-parse", f"refs/heads/{head}", cwd=self.origin)
        tree = self.git("merge-tree", "--write-tree", into, other, cwd=self.origin)
        identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
        merge = self.git(*identity, "commit-tree", tree, "-p", into, "-p", other, "-m", f"Merge {head}", cwd=self.origin)
        self.git("update-ref", f"refs/heads/{base}", merge, into, cwd=self.origin)

    def cleanup(self) -> None:
        self._tmp.cleanup()
