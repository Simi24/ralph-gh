import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor.git_adapter import GitCli, GitError, git_output
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

    def test_remote_sha_reads_the_fetched_branch_tip(self) -> None:
        second = self.commit_on_main("second.txt")
        self.repo.git("update-ref", "-d", "refs/remotes/origin/main")  # a stale clone: remote_sha fetches
        self.assertEqual(self.cli.remote_sha("main"), second)
        with self.assertRaises(GitError):
            self.cli.remote_sha("no-such-branch")

    def test_a_detached_worktree_is_pinned_to_its_sha(self) -> None:
        first = self.repo.git("rev-parse", "HEAD")
        self.commit_on_main("second.txt")
        path = self.repo.root / "pinned"
        self.cli.add_detached_worktree(path, first)
        self.assertEqual(self.repo.git("rev-parse", "HEAD", cwd=path), first)  # not the tip
        self.cli.remove_detached_worktree(path)
        self.assertFalse(path.exists())

    def test_a_detached_worktree_refuses_anything_but_a_full_sha(self) -> None:
        for bad in ("main", "HEAD", "--detach", "abc123"):
            with self.assertRaises(GitError):
                self.cli.add_detached_worktree(self.repo.root / "bad", bad)

    def test_remove_stale_worktrees_clears_what_a_crash_left_and_their_branches(self) -> None:
        trees = self.repo.root / "worktrees"
        self.cli.add_worktree(trees / "ticket-7", "feat-ticket-7", "main")
        self.cli.add_worktree(trees / "integration-fix", "feat-int-integration-fix", "main")
        self.cli.add_detached_worktree(trees / "integration-verify", self.repo.git("rev-parse", "HEAD"))
        (trees / "ticket-7" / "dirty.txt").write_text("uncommitted\n")  # --force removes it anyway
        outside = self.repo.root / "outside"
        self.cli.add_worktree(outside, "keep-me", "main")  # not under `trees`: untouched
        (trees / "unregistered").mkdir()  # a directory git no longer knows about
        (trees / "unregistered" / "junk").write_text("x\n")
        self.cli.remove_stale_worktrees(trees)
        self.assertEqual(list(trees.iterdir()), [])
        branches = self.repo.git("branch", "--format=%(refname:short)").split()
        self.assertNotIn("feat-ticket-7", branches)
        self.assertNotIn("feat-int-integration-fix", branches)
        self.assertIn("keep-me", branches)
        self.assertTrue(outside.exists())
        self.assertNotIn("ticket-7", self.repo.git("worktree", "list"))
        self.cli.add_worktree(trees / "ticket-7", "feat-ticket-7", "main")  # the directory is usable again

    def test_remove_stale_worktrees_is_a_no_op_without_the_directory(self) -> None:
        self.cli.remove_stale_worktrees(self.repo.root / "never-created")

    def test_remove_stale_worktrees_prunes_a_worktree_whose_directory_is_gone(self) -> None:
        trees = self.repo.root / "worktrees"
        self.cli.add_worktree(trees / "ticket-8", "feat-ticket-8", "main")
        shutil.rmtree(trees / "ticket-8")
        self.cli.remove_stale_worktrees(trees)
        self.assertNotIn("ticket-8", self.repo.git("worktree", "list"))
        self.assertNotIn("feat-ticket-8", self.repo.git("branch", "--format=%(refname:short)").split())

    def test_delete_remote_branch_deletes_and_tolerates_a_missing_branch(self) -> None:
        self.repo.git("push", "origin", "HEAD:refs/heads/doomed")
        self.cli.delete_remote_branch("doomed")
        self.assertEqual(self.repo.git("ls-remote", "--heads", "origin", "doomed"), "")
        self.cli.delete_remote_branch("doomed")  # already gone: not an error

    def test_a_branch_name_that_looks_like_an_option_is_data(self) -> None:
        with self.assertRaises(GitError):
            self.cli.add_worktree(self.repo.root / "wt", "work", "--upload-pack=x")

    def test_git_runs_in_its_own_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "git"
            stub.write_text("#!/bin/sh\nps -o pgid= -p $$\n")
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            with mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ['PATH']}"}):
                group = int(git_output("status", cwd=self.repo.checkout))
        self.assertNotEqual(group, os.getpgrp())


if __name__ == "__main__":
    unittest.main()
