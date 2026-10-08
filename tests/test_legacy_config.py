import tempfile
import unittest
from pathlib import Path

from conductor.config import ConfigError, parse_config
from conductor.legacy_config import MAPPING, load_repo_config


class LegacyConfigTest(unittest.TestCase):
    def test_only_old_config_prints_the_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".ralph-gh.config").write_text("RALPH_VERIFY_COMMANDS=(true)\n")
            with self.assertRaises(ConfigError) as ctx:
                load_repo_config(Path(tmp))
        message = str(ctx.exception)
        for old, new, _ in MAPPING:
            self.assertIn(f"{old} -> {new}", message)
        self.assertIn("RALPH_GATE_AGENT -> reviewer_agent", message)

    def test_toml_wins_when_both_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".ralph-gh.config").write_text("x")
            (Path(tmp) / ".ralph-gh.toml").write_text('verify_commands = ["true"]\n')
            self.assertEqual(load_repo_config(Path(tmp)).verify_commands, ("true",))

    def test_invalid_values_name_the_key(self) -> None:
        for key, value in [
            ("preflight_health_retries", 0),
            ("preflight_health_retries", "many"),
            ("parallel", 0),
            ("parallel", True),
            ("preflight_command", 3),
        ]:
            with self.subTest(key, value=value), self.assertRaises(ConfigError) as ctx:
                parse_config({"verify_commands": ["x"], key: value})
            self.assertIn(key, str(ctx.exception))

    def test_preflight_defaults(self) -> None:
        config = parse_config({"verify_commands": ["x"]})
        self.assertEqual((config.preflight_command, config.preflight_health_retries), ("", 30))


if __name__ == "__main__":
    unittest.main()
