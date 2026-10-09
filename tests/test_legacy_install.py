"""A pre-plugin `install.sh` installation is reported with removal steps and never executed."""
import shlex
import tempfile
import unittest
from pathlib import Path

from conductor.legacy_install import legacy_install_report


class LegacyInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name)

    def touch(self, relative: str) -> Path:
        path = self.config / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        return path

    def test_a_clean_config_dir_has_nothing_to_report(self) -> None:
        self.assertIsNone(legacy_install_report(self.config))

    def test_run_state_alone_is_not_a_legacy_install(self) -> None:
        self.touch("ralph-gh/state/Me__Repo/run.log")
        self.assertIsNone(legacy_install_report(self.config))

    def test_the_old_conductor_and_launcher_are_reported_with_exact_removal_steps(self) -> None:
        self.touch("ralph-gh/ralph-gh")
        self.touch("ralph-gh/conductor/cli.py")
        self.touch("ralph-gh/.installed")
        report = legacy_install_report(self.config) or ""
        self.assertIn(f"rm -rf {shlex.quote(f"{self.config}/ralph-gh/conductor")}", report)
        self.assertIn(f"rm -f {shlex.quote(f"{self.config}/ralph-gh/ralph-gh")}", report)
        self.assertIn(f"rm -f {shlex.quote(f"{self.config}/ralph-gh/.installed")}", report)
        self.assertIn("/plugin install ralph-gh --marketplace Simi24/ralph-gh", report)
        self.assertNotIn(f"'{self.config}/ralph-gh'\n", report)  # never the whole dir: it holds run state

    def test_only_what_is_present_is_listed(self) -> None:
        self.touch("ralph-gh/ralph-gh")
        report = legacy_install_report(self.config) or ""
        self.assertNotIn("conductor", report.replace("ralph-gh/ralph-gh", ""))
        self.assertNotIn(".installed", report)

    def test_the_old_user_level_agents_and_skill_are_reported(self) -> None:
        self.touch("agents/ralph-gate-reviewer.md")
        self.touch("agents/ralph-ticket-gate.md")
        self.touch("skills/ralph-gh/SKILL.md")
        report = legacy_install_report(self.config) or ""
        self.assertIn(f"rm -f {shlex.quote(f"{self.config}/agents/ralph-gate-reviewer.md")}", report)
        self.assertIn(f"rm -f {shlex.quote(f"{self.config}/agents/ralph-ticket-gate.md")}", report)
        self.assertIn(f"rm -rf {shlex.quote(f"{self.config}/skills/ralph-gh")}", report)

    def test_a_path_with_a_quote_cannot_break_out_of_the_suggested_command(self) -> None:
        config = self.config / "it's"
        (config / "ralph-gh").mkdir(parents=True)
        (config / "ralph-gh/ralph-gh").write_text("x")
        report = legacy_install_report(config) or ""
        self.assertIn("'\"'\"'", report)  # shlex-style quoting


if __name__ == "__main__":
    unittest.main()
