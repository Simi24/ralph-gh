"""The README's one terminal step makes `ralph-gh` run the installed plugin's launcher."""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STEP = re.compile(r"<!-- terminal-launcher -->\s*```bash\n(.*?)\n```", re.S)


class TerminalLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        match = STEP.search((REPO / "README.md").read_text())
        self.assertIsNotNone(match, "README has no marked terminal-launcher step")
        self.step = match.group(1)  # type: ignore[union-attr]

    def install_version(self, version: str, config: Path | None = None) -> None:
        launcher = (config or self.home / ".claude") / "plugins/cache/ralph-gh/ralph-gh" / version / "ralph-gh"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(f'#!/bin/sh\necho "plugin {version} $@"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)

    def env(self, **extra: str) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if k not in ("HOME", "CLAUDE_CONFIG_DIR")}
        return {**base, "HOME": str(self.home), **extra}

    def take_the_step(self) -> Path:
        done = subprocess.run(["bash", "-c", self.step], env=self.env(), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.home / ".local/bin/ralph-gh"

    def call(self, command: Path, *argv: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(command), *argv], env=self.env(**env), capture_output=True, text=True)

    def test_the_command_runs_the_installed_plugin_with_its_arguments(self) -> None:
        self.install_version("1.0.0")
        out = self.call(self.take_the_step(), "run", "--prd", "7")
        self.assertEqual(out.stdout.strip(), "plugin 1.0.0 run --prd 7")

    def test_it_follows_plugin_updates_without_repeating_the_step(self) -> None:
        self.install_version("1.9.0")
        command = self.take_the_step()
        self.install_version("1.10.0")
        self.assertEqual(self.call(command, "stop").stdout.strip(), "plugin 1.10.0 stop")

    def test_it_follows_claude_config_dir(self) -> None:
        elsewhere = self.home / "cfg"
        self.install_version("2.0.0", elsewhere)
        command = self.take_the_step()
        self.assertEqual(self.call(command, "stop", CLAUDE_CONFIG_DIR=str(elsewhere)).stdout.strip(), "plugin 2.0.0 stop")

    def test_without_the_plugin_it_says_so_and_fails(self) -> None:
        out = self.call(self.take_the_step(), "run")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("/plugin install ralph-gh", out.stderr)


if __name__ == "__main__":
    unittest.main()
