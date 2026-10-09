"""The configs this repo ships are valid, and the retired bash files are gone."""
import unittest
from dataclasses import fields
from pathlib import Path


from conductor.config import Config, load_config
from conductor.legacy_config import load_repo_config

REPO = Path(__file__).resolve().parent.parent


class ShippedConfigsTest(unittest.TestCase):
    def test_repo_config_loads_and_runs_the_unit_tests_and_the_installer_check(self) -> None:
        config = load_config(REPO / ".ralph-gh.toml")
        self.assertEqual(
            config.verify_commands,
            (
                "python3 -m unittest discover -s tests -t .",
                "bash -n install.sh",
                "claude plugin validate .",
                "claude plugin test .",
                "claude plugin validate worker-bundle",
                "claude plugin test worker-bundle",
            ),
        )

    def test_example_config_loads_and_documents_every_key(self) -> None:
        path = REPO / "example.ralph-gh.toml"
        load_config(path)
        text = path.read_text()
        run_only = {"prd", "repo_root", "state_root", "autonomy"}
        for field in fields(Config):
            if field.name not in run_only:
                with self.subTest(field.name):
                    self.assertRegex(text, rf"(?m)^#? ?{field.name} = ")

    def test_repo_is_served_by_the_toml_not_the_legacy_file(self) -> None:
        self.assertEqual(load_repo_config(REPO).verify_commands, load_config(REPO / ".ralph-gh.toml").verify_commands)
        self.assertFalse((REPO / ".ralph-gh.config").exists())
        self.assertFalse((REPO / "example.ralph-gh.config").exists())

    def test_the_bash_orchestrator_and_its_prompt_are_gone(self) -> None:
        self.assertFalse((REPO / "ralph-gh.sh").exists())
        self.assertFalse((REPO / "prompts/iteration.md").exists())
        self.assertEqual((REPO / "CLAUDE.md").read_text().strip(), "@AGENTS.md")


if __name__ == "__main__":
    unittest.main()
