"""The docs describe the plugin model, and install.sh appears only as a migration note."""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text()
SMOKE = (REPO / "docs" / "smoke-test.md").read_text()


class ReadmeTest(unittest.TestCase):
    def test_documents_install_update_launcher_guard_dashboard_and_minimum_version(self) -> None:
        for needle in (
            "/plugin install ralph-gh --marketplace Simi24/ralph-gh",
            "claude plugin update",
            "Run it from a terminal",
            "ralph-guard",
            "allow_manifest_edits",
            "defense in depth",
            "/ralph",
            "Minimum Claude Code version",
        ):
            self.assertIn(needle, README)

    def test_per_repo_setup_does_not_copy_from_a_legacy_install_path(self) -> None:
        self.assertNotIn("cp ~/.claude/ralph-gh/example", README)


class SmokeTestDocTest(unittest.TestCase):
    def test_installs_the_plugin_checks_the_guard_by_effects_and_opens_the_dashboard(self) -> None:
        for needle in ("/plugin install", "worker-bundle", "RALPH_GUARD=1", "git ls-remote", "/ralph"):
            self.assertIn(needle, SMOKE)

    def test_does_not_use_the_retired_agents_directory_or_a_path_to_the_checkout(self) -> None:
        self.assertNotIn("$RALPH_SRC/agents", SMOKE)
        self.assertNotIn("RALPH_SRC/ralph-gh", SMOKE)


class InstallShMentionTest(unittest.TestCase):
    def test_docs_mention_install_sh_only_in_the_migration_section(self) -> None:
        sections = re.split(r"(?m)^(?=#{1,3} )", README)
        for section in sections:
            if "install.sh" in section:
                self.assertTrue(section.startswith("### Moving from"), section[:60])
        for name in ("docs/smoke-test.md", "example.ralph-gh.toml", "skills/ralph-gh/SKILL.md"):
            self.assertNotIn("install.sh", (REPO / name).read_text(), name)


if __name__ == "__main__":
    unittest.main()
