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
        self.assertEqual(config.reviewer_agent, "ralph-guard:ralph-gate-reviewer")

    def test_allow_manifest_edits_defaults_to_false_and_must_be_a_bool(self) -> None:
        self.assertFalse(self.load('verify_commands = ["x"]').allow_manifest_edits)
        self.assertTrue(self.load('verify_commands = ["x"]\nallow_manifest_edits = true').allow_manifest_edits)
        with self.assertRaises(ConfigError):
            self.load('verify_commands = ["x"]\nallow_manifest_edits = "yes"')

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
            ("allowlist not a list", 'verify_commands = ["x"]\nyolo_allowlist = "^src/"'),
            ("allowlist bad regex", 'verify_commands = ["x"]\nyolo_allowlist = ["("]'),
        ]:
            with self.subTest(name), self.assertRaises(ConfigError):
                self.load(toml)

    def test_ref_like_values_are_data_never_options(self) -> None:
        for key, bad in [
            ("base_branch", "--upload-pack=x"),
            ("base_branch", "main..evil"),
            ("base_branch", "ma in"),
            ("base_branch", "main;rm"),
            ("branch_prefix", "-feat"),
            ("reviewer_agent", "--dangerous"),
            ("ticket_gate_agent", "-x"),
            ("model", "--help"),
            ("model", "a b"),
        ]:
            with self.subTest(key=key, bad=bad), self.assertRaisesRegex(ConfigError, key):
                self.load(f'verify_commands = ["x"]\n{key} = "{bad}"')

    def test_real_refs_agents_and_models_are_accepted(self) -> None:
        config = self.load(
            'verify_commands = ["x"]\nbase_branch = "release/1.2_x"\nmodel = "claude-opus-4[1m]"\n'
            'reviewer_agent = "plugin:reviewer"'
        )
        self.assertEqual((config.base_branch, config.model), ("release/1.2_x", "claude-opus-4[1m]"))

    def test_yolo_allowlist_is_a_list_of_regexes_and_empty_by_default(self) -> None:
        self.assertEqual(self.load('verify_commands = ["x"]').yolo_allowlist, ())
        config = self.load('verify_commands = ["x"]\nyolo_allowlist = ["^src/", "^README\\\\.md$"]')
        self.assertEqual(config.yolo_allowlist, ("^src/", "^README\\.md$"))

    def test_autonomy_defaults_to_respect_hitl_arch_and_unknown_modes_are_refused(self) -> None:
        config = self.load('verify_commands = ["x"]')
        run_args = dict(prd=1, repo_root=Path("."), state_root=Path("."))
        self.assertEqual(config.with_run(**run_args).autonomy, "respect-hitl-arch")
        self.assertEqual(config.with_run(**run_args, autonomy="yolo").autonomy, "yolo")
        with self.assertRaises(ConfigError):
            config.with_run(**run_args, autonomy="YOLO")
        with self.assertRaises(ConfigError):
            self.load('verify_commands = ["x"]\nautonomy = "yolo"')

    def test_missing_file_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(Path("/nonexistent/.ralph-gh.toml"))


if __name__ == "__main__":
    unittest.main()
