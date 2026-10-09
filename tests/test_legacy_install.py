"""A legacy `install.sh` installation is reported with removal steps and never executed."""
import tempfile
import unittest
from pathlib import Path

from conductor.legacy_install import legacy_install_notice


class LegacyInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name).resolve() / ".claude"
        self.config.mkdir()

    def touch(self, rel: str) -> None:
        path = self.config / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")

    def test_a_clean_config_dir_reports_nothing(self) -> None:
        self.assertIsNone(legacy_install_notice(self.config))

    def test_run_state_alone_is_not_a_legacy_install(self) -> None:
        self.touch("ralph-gh/state/Me__Repo/run.log")
        self.assertIsNone(legacy_install_notice(self.config))

    def test_every_deployed_piece_is_named_with_its_removal(self) -> None:
        for rel in ("ralph-gh/conductor/cli.py", "ralph-gh/ralph-gh", "skills/ralph-gh/SKILL.md", "agents/ralph-ticket-gate.md"):
            self.touch(rel)
        self.touch("ralph-gh/state/Me__Repo/run.log")
        notice = legacy_install_notice(self.config) or ""
        for rel in ("ralph-gh/conductor", "ralph-gh/ralph-gh", "skills/ralph-gh", "agents/ralph-ticket-gate.md"):
            self.assertIn(f"rm -rf '{self.config / rel}'", notice)
        self.assertNotIn("rm -rf '" + str(self.config / "ralph-gh/state"), notice)  # run state is kept
        self.assertNotIn("ralph-gate-reviewer", notice)  # only what is actually there

    def test_a_dangling_symlink_still_counts(self) -> None:
        (self.config / "ralph-gh").mkdir()
        (self.config / "ralph-gh/ralph-gh").symlink_to(self.config / "nowhere")
        self.assertIn("ralph-gh/ralph-gh", legacy_install_notice(self.config) or "")


if __name__ == "__main__":
    unittest.main()
