"""The install drift warning: advisory, fails open, never touches the network."""
import tempfile
import unittest
from pathlib import Path

from conductor.drift import current_sha, drift_warning
from tests.gitrepo import TempRepo

OLD = "a" * 40
NEW = "b" * 40


class DriftWarningTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install = Path(self._tmp.name) / "install"
        self.clone = Path(self._tmp.name) / "clone"
        self.install.mkdir()
        self.clone.mkdir()

    def stamp(self, source_path: object, source_sha: str) -> None:
        (self.install / ".installed").write_text(f"source_path={source_path}\nsource_sha={source_sha}\n")

    def test_behind_the_clone_warns_with_both_short_shas(self) -> None:
        self.stamp(self.clone, OLD)
        warning = drift_warning(self.install, lambda path: NEW)
        self.assertEqual(warning, "installed copy is behind your clone (aaaaaaa -> bbbbbbb) — run install.sh")

    def test_same_commit_is_silent(self) -> None:
        self.stamp(self.clone, OLD)
        self.assertIsNone(drift_warning(self.install, lambda path: OLD))

    def test_every_missing_piece_fails_open(self) -> None:
        self.assertIsNone(drift_warning(self.install, lambda path: NEW))  # no .installed at all
        self.stamp(self.clone, "unknown")
        self.assertIsNone(drift_warning(self.install, lambda path: NEW))  # source was not a git checkout
        self.stamp(self.install / "gone", OLD)
        self.assertIsNone(drift_warning(self.install, lambda path: NEW))  # the clone was moved or deleted
        self.stamp(self.clone, OLD)
        self.assertIsNone(drift_warning(self.install, lambda path: ""))  # the clone is not readable as git
        (self.install / ".installed").write_text("garbage\n")
        self.assertIsNone(drift_warning(self.install, lambda path: NEW))

    def test_current_sha_reads_a_real_checkout_and_is_empty_elsewhere(self) -> None:
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        self.assertEqual(current_sha(repo.checkout), repo.git("rev-parse", "HEAD"))
        self.assertEqual(current_sha(self.clone), "")  # not a git checkout (the temp dir is outside any repo)


if __name__ == "__main__":
    unittest.main()
