import tempfile
import unittest
from pathlib import Path

from conductor.config import ConfigError, load_config


class LoadConfigTest(unittest.TestCase):
    def load(self, toml: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".ralph-gh.toml"
            path.write_text(toml)
            return load_config(path)

    def test_reads_values_as_data_and_applies_defaults(self) -> None:
        config = self.load('verify_commands = ["echo $(touch pwned)"]\nbranch_prefix = "ralph"\nparallel = 1\n')
        self.assertEqual(config.verify_commands, ("echo $(touch pwned)",))
        self.assertEqual((config.branch_prefix, config.parallel, config.base_branch), ("ralph", 1, "main"))
        self.assertEqual(config.reviewer_agent, "ralph-gate-reviewer")

    def test_invalid_configs_are_refused(self) -> None:
        for name, toml in [
            ("missing verify", 'base_branch = "main"'),
            ("empty verify", "verify_commands = []"),
            ("unknown key", 'verify_commands = ["x"]\nverfy = 1'),
            ("bad type", 'verify_commands = ["x"]\nparallel = "3"'),
            ("zero parallel", 'verify_commands = ["x"]\nparallel = 0'),
            ("bad prefix", 'verify_commands = ["x"]\nbranch_prefix = "Bad Prefix"'),
            ("bad agent", 'verify_commands = ["x"]\nreviewer_agent = "a b; rm"'),
            ("not toml", "verify_commands = ["),
        ]:
            with self.subTest(name), self.assertRaises(ConfigError):
                self.load(toml)

    def test_missing_file_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(Path("/nonexistent/.ralph-gh.toml"))


if __name__ == "__main__":
    unittest.main()
