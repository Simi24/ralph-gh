"""install.sh is a migration stub, run only against a temporary HOME and CLAUDE_CONFIG_DIR."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class InstallStubTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "home"
        self.config = self.home / ".claude"
        (self.config / "ralph-gh/conductor").mkdir(parents=True)
        (self.config / "ralph-gh/state").mkdir()
        (self.config / "ralph-gh/conductor/cli.py").write_text("legacy")

    def run_stub(self, **env: str) -> subprocess.CompletedProcess[str]:
        base = {k: v for k, v in os.environ.items() if k not in ("HOME", "CLAUDE_CONFIG_DIR")}
        return subprocess.run(
            ["bash", str(REPO / "install.sh")], env={**base, "HOME": str(self.home), **env}, capture_output=True, text=True
        )

    def snapshot(self) -> list[str]:
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))

    def test_it_prints_the_plugin_install_command_and_the_removal_steps_and_exits_non_zero(self) -> None:
        proc = self.run_stub()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("/plugin install ralph-gh --marketplace Simi24/ralph-gh", proc.stderr)
        self.assertIn(f"rm -rf \"{self.config}/ralph-gh/conductor\"", proc.stderr.replace("'", '"'))
        self.assertIn("keep", proc.stderr)

    def test_it_installs_and_removes_nothing(self) -> None:
        before = self.snapshot()
        self.run_stub()
        self.assertEqual(self.snapshot(), before)

    def test_it_follows_claude_config_dir(self) -> None:
        proc = self.run_stub(CLAUDE_CONFIG_DIR=str(self.home / "elsewhere"))
        self.assertIn(f"{self.home}/elsewhere/ralph-gh/conductor", proc.stderr)

    def test_it_is_valid_bash(self) -> None:
        self.assertEqual(subprocess.run(["bash", "-n", str(REPO / "install.sh")]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
