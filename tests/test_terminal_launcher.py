"""The one documented terminal step (a README shell function) runs the installed plugin's conductor."""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def documented_function() -> str:
    readme = (REPO / "README.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)```", readme, re.S)
    found = [b for b in blocks if b.startswith("ralph-gh()")]
    assert len(found) == 1, "README must document exactly one `ralph-gh()` terminal step"
    return found[0]


class TerminalLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plugin = self.tmp / "cache/ralph-gh/9.9.9"
        self.plugin.mkdir(parents=True)
        launcher = self.plugin / "ralph-gh"
        launcher.write_text('#!/bin/sh\necho "plugin conductor: $@"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        listing = [
            {"id": "other@market", "installPath": str(self.tmp / "elsewhere")},
            {"id": "ralph-gh@ralph-gh", "installPath": str(self.plugin)},
        ]
        claude = bin_dir / "claude"
        claude.write_text(f"#!/bin/sh\n[ \"$*\" = 'plugin list --json' ] && cat <<'EOF'\n{json.dumps(listing)}\nEOF\n")
        claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def shell(self, command: str) -> subprocess.CompletedProcess[str]:
        script = f"{documented_function()}\n{command}"
        return subprocess.run(["bash", "-c", script], env=self.env, capture_output=True, text=True)

    def test_run_and_stop_reach_the_installed_plugins_launcher(self) -> None:
        done = self.shell("ralph-gh run --prd 7; ralph-gh stop")
        self.assertEqual(done.stdout.splitlines(), ["plugin conductor: run --prd 7", "plugin conductor: stop"])

    def test_the_function_works_in_zsh_syntax_too(self) -> None:
        self.assertNotIn("function ", documented_function().split("\n")[0])  # plain POSIX definition

    def test_a_missing_plugin_is_a_clear_error_and_runs_nothing(self) -> None:
        (self.tmp / "bin/claude").write_text("#!/bin/sh\necho '[]'\n")
        done = self.shell("ralph-gh run --prd 7")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("plugin conductor", done.stdout)
        self.assertIn("plugin", done.stderr)


if __name__ == "__main__":
    unittest.main()
