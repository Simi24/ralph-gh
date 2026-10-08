"""install.sh, run only against a temporary HOME and CLAUDE_CONFIG_DIR.

Never point these tests at the real ~/.claude: `Installer.run` always passes
both variables explicitly, so the real home is never an install target.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RETIRED_AGENT = "ralph-refactorer.md"


class Installer:
    def __init__(self, tmp: Path, config_dir: Path | None = None) -> None:
        self.home = tmp / "home"
        self.config = config_dir or self.home / ".claude"
        self.home.mkdir(exist_ok=True)

    @property
    def dest(self) -> Path:
        return self.config / "ralph-gh"

    def run(self, *, set_config_dir: bool = True) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in ("HOME", "CLAUDE_CONFIG_DIR")}
        env["HOME"] = str(self.home)
        if set_config_dir:
            env["CLAUDE_CONFIG_DIR"] = str(self.config)
        return subprocess.run(["bash", str(REPO / "install.sh")], env=env, capture_output=True, text=True)


class InstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.install = Installer(self.tmp)

    def ok(self, **kwargs: bool) -> subprocess.CompletedProcess[str]:
        proc = self.install.run(**kwargs)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_installs_package_launcher_agents_skill_and_docs(self) -> None:
        self.ok()
        dest, config = self.install.dest, self.install.config
        self.assertTrue(os.access(dest / "ralph-gh", os.X_OK))
        for module in (REPO / "conductor").glob("*.py"):
            self.assertEqual((dest / "conductor" / module.name).read_bytes(), module.read_bytes())
        for agent in (REPO / "agents").glob("*.md"):
            self.assertEqual((config / "agents" / agent.name).read_bytes(), agent.read_bytes())
        self.assertEqual((config / "skills/ralph-gh/SKILL.md").read_bytes(), (REPO / "skills/ralph-gh/SKILL.md").read_bytes())
        self.assertTrue((dest / "example.ralph-gh.toml").is_file())
        self.assertTrue((dest / "README.md").is_file())
        self.assertFalse((dest / "conductor/__pycache__").exists() and any((dest / "conductor/__pycache__").iterdir()))

    def test_the_installed_launcher_runs(self) -> None:
        self.ok()
        proc = subprocess.run([str(self.install.dest / "ralph-gh"), "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run", proc.stdout)

    def test_home_dot_claude_is_the_default_target(self) -> None:
        self.ok(set_config_dir=False)
        self.assertTrue((self.install.home / ".claude/ralph-gh/ralph-gh").is_file())

    def test_claude_config_dir_is_honoured(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        self.install = Installer(self.tmp, elsewhere)
        self.ok()
        self.assertTrue((elsewhere / "ralph-gh/ralph-gh").is_file())
        self.assertFalse((self.install.home / ".claude").exists())

    def test_a_rerun_leaves_no_backups(self) -> None:
        self.ok()
        proc = self.ok()
        self.assertEqual(list(self.install.config.rglob("*.bak")), [])
        self.assertNotIn("backed up", proc.stdout)

    def test_a_differing_installed_file_is_backed_up_then_replaced(self) -> None:
        self.ok()
        targets = [
            self.install.dest / "conductor/config.py",
            self.install.dest / "ralph-gh",
            self.install.config / "agents/ralph-gate-reviewer.md",
            self.install.config / "skills/ralph-gh/SKILL.md",
            self.install.dest / "example.ralph-gh.toml",
        ]
        for target in targets:
            target.write_text("local edit\n")
        proc = self.ok()
        for target in targets:
            with self.subTest(target.name):
                self.assertEqual(Path(f"{target}.bak").read_text(), "local edit\n")
                self.assertNotEqual(target.read_text(), "local edit\n")
                self.assertIn(f"{target.name} differs", proc.stdout)

    def test_retired_files_are_backed_up_and_no_longer_active(self) -> None:
        self.ok()
        agent = self.install.config / "agents" / RETIRED_AGENT
        stale_module = self.install.dest / "conductor/old_module.py"
        old_script = self.install.dest / "ralph-gh.sh"
        old_prompt = self.install.dest / "CLAUDE.md"
        for path in (agent, stale_module, old_script, old_prompt):
            path.write_text("old\n")
        self.ok()
        for path in (agent, stale_module, old_script, old_prompt):
            with self.subTest(path.name):
                self.assertFalse(path.exists())
                self.assertEqual(Path(f"{path}.bak").read_text(), "old\n")

    def test_stamps_the_source_for_the_drift_warning(self) -> None:
        self.ok()
        stamp = dict(
            line.split("=", 1) for line in (self.install.dest / ".installed").read_text().splitlines()
        )
        self.assertEqual(stamp["source_path"], str(REPO))
        self.assertRegex(stamp["source_sha"], r"^([0-9a-f]{40}|unknown)$")

    def test_a_clone_without_a_release_clears_a_stale_version(self) -> None:
        self.ok()
        version = self.install.dest / "version.txt"
        version.write_text("9.9.9\n")
        self.ok()
        self.assertEqual(version.exists(), (REPO / "version.txt").exists())

    def test_an_old_python_is_refused_before_anything_is_written(self) -> None:
        fake_bin = self.tmp / "bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text("#!/bin/sh\nexit 1\n")
        fake_python.chmod(0o755)
        env = {**os.environ, "HOME": str(self.install.home), "CLAUDE_CONFIG_DIR": str(self.install.config)}
        env["PATH"] = f"{fake_bin}{os.pathsep}{os.environ['PATH']}"
        proc = subprocess.run(["bash", str(REPO / "install.sh")], env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Python 3.12", proc.stderr)
        self.assertFalse(self.install.config.exists())


if __name__ == "__main__":
    unittest.main()
