import unittest

from conductor.git_adapter import GitCli
from tests.gitrepo import TempRepo


class GitCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.cli = GitCli(self.repo.checkout)

    def commit_on_main(self, name: str) -> str:
        (self.repo.checkout / name).write_text("x\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-m", f"add {name}")
        self.repo.git("push", "origin", "HEAD:refs/heads/main")
        return self.repo.git("rev-parse", "HEAD")

    def test_is_behind_tracks_origin_base(self) -> None:
        path = self.repo.root / "wt"
        self.cli.add_worktree(path, "work", "main")
        self.assertFalse(self.cli.is_behind(path, "main"))
        self.commit_on_main("new.txt")
        self.assertTrue(self.cli.is_behind(path, "main"))

    def test_is_ancestor_true_only_for_history(self) -> None:
        first = self.repo.git("rev-parse", "HEAD")
        second = self.commit_on_main("second.txt")
        self.assertTrue(self.cli.is_ancestor(first, second))
        self.assertFalse(self.cli.is_ancestor(second, first))

    def test_is_ancestor_fails_closed(self) -> None:
        head = self.repo.git("rev-parse", "HEAD")
        self.assertFalse(self.cli.is_ancestor("main", head))
        self.assertFalse(self.cli.is_ancestor(head, "HEAD; rm -rf /"))
        self.assertFalse(self.cli.is_ancestor(head, "0" * 40))  # not fetchable


if __name__ == "__main__":
    unittest.main()
