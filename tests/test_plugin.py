"""The repo is a Claude Code marketplace listing one plugin; the conductor finds its own files."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


class PluginLayoutTest(unittest.TestCase):
    def test_marketplace_lists_the_one_ralph_gh_plugin_from_the_repo_root(self) -> None:
        marketplace = _json(REPO / ".claude-plugin/marketplace.json")
        self.assertEqual(marketplace["name"], "ralph-gh")
        self.assertEqual([(p["name"], p["source"]) for p in marketplace["plugins"]], [("ralph-gh", "./")])

    def test_plugin_version_tracks_the_release_manifest(self) -> None:
        released = _json(REPO / ".release-please-manifest.json")["."]
        self.assertEqual(_json(REPO / ".claude-plugin/plugin.json")["version"], released)

    def test_release_please_bumps_the_plugin_manifest(self) -> None:
        config = _json(REPO / "release-please-config.json")["packages"]["."]
        paths = [extra["path"] for extra in config["extra-files"]]
        self.assertIn(".claude-plugin/plugin.json", paths)

    def test_the_skill_is_shipped_and_starts_runs_from_the_plugin_root(self) -> None:
        text = (REPO / "skills/ralph-gh/SKILL.md").read_text()
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/ralph-gh", text)
        for stale in (".claude}/ralph-gh/ralph-gh", ".claude}/ralph-gh/README", ".claude}/ralph-gh/example"):
            self.assertNotIn(stale, text)

    def test_the_skill_accepts_a_leading_run_or_stop_subcommand(self) -> None:
        text = (REPO / "skills/ralph-gh/SKILL.md").read_text()
        self.assertRegex(text, r"starts with the subcommand `run` or `stop`, forward it as is")
        self.assertIn("/ralph-gh run --prd 52", text)
        self.assertNotIn("`/ralph-gh --prd", text)

    def test_the_skill_teaches_the_dashboard(self) -> None:
        text = (REPO / "skills/ralph-gh/SKILL.md").read_text()
        for needed in ("/ralph owner/repo", "/ralph off", "Drain", "nothing polls", "instead of reading `run.log`"):
            self.assertIn(needed, text)
        self.assertRegex(text, r"(?i)offer the user `/ralph` right after")

    def test_the_skill_keeps_graceful_and_immediate_stop(self) -> None:
        text = (REPO / "skills/ralph-gh/SKILL.md").read_text()
        for needed in ("ralph-gh stop", "STOP", "Ctrl-C", "SIGTERM"):
            self.assertIn(needed, text)

    def test_worker_agents_are_not_discoverable_by_the_plugin(self) -> None:
        self.assertFalse((REPO / "agents").exists())
        for name in ("ralph-gate-reviewer", "ralph-ticket-gate"):
            self.assertTrue((REPO / "worker-bundle/agents" / f"{name}.md").is_file())


class RelocatedInstallTest(unittest.TestCase):
    """The CLI entry works from an install dir that is not the clone, with no path configured."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.install = Path(tmp.name).resolve() / "cache/ralph-gh"
        self.install.mkdir(parents=True)
        shutil.copytree(REPO / "conductor", self.install / "conductor", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(REPO / "ralph-gh", self.install / "ralph-gh")

    def test_launcher_runs_the_package_next_to_it(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run(
            [str(self.install / "ralph-gh"), "--help"], cwd=self.install.parent, env=env, capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run", proc.stdout)

    def test_the_package_comes_from_the_install_dir_even_through_a_symlink(self) -> None:
        link = self.install.parent / "bin-ralph-gh"
        link.symlink_to(self.install / "ralph-gh")
        proc = subprocess.run(
            [str(link), "stop", "--help"], cwd=self.install.parent, capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
