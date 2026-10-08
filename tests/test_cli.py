import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import cli, labels
from conductor.preflight import REQUIRED_LABELS
from conductor.repo_context import RepoContext, RepoError
from conductor.result import RunResult
from conductor.state_dir import default_state_root


class StateDirTest(unittest.TestCase):
    def test_default_is_under_the_claude_dir(self) -> None:
        root = default_state_root("Me/Repo", {}, Path("/home/u"))
        self.assertEqual(root, Path("/home/u/.claude/ralph-gh/state/Me__Repo"))

    def test_claude_config_dir_is_honoured(self) -> None:
        root = default_state_root("Me/Repo", {"CLAUDE_CONFIG_DIR": "/cfg"}, Path("/home/u"))
        self.assertEqual(root, Path("/cfg/ralph-gh/state/Me__Repo"))

    def test_odd_repo_names_are_refused(self) -> None:
        for bad in ("", "x", "a/b/c", "../x", "a/..", "a b/c"):
            with self.subTest(bad), self.assertRaises(ValueError):
                default_state_root(bad, {}, Path("/h"))


class LabelsTest(unittest.TestCase):
    def test_every_label_constant_is_created_at_preflight(self) -> None:
        constants = {v for k, v in vars(labels).items() if k.isupper() and isinstance(v, str)}
        self.assertLessEqual(constants, {name for name, _, _ in REQUIRED_LABELS})


class CliTest(unittest.TestCase):
    def parse_error(self, *argv: str) -> int:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(list(argv))
        return int(raised.exception.code)  # type: ignore[arg-type]

    def test_usage_errors_exit_2(self) -> None:
        for argv in (["run"], ["run", "--prd", "x"], ["run", "--prd", "0"], ["run", "--prd", "1", "--parallel", "0"],
                     ["run", "--prd", "1", "--autonomy", "nope"], ["bogus"], []):
            with self.subTest(argv):
                self.assertEqual(self.parse_error(*argv), 2)

    def test_outside_a_repo_is_a_startup_error(self) -> None:
        with mock.patch.object(cli, "find_repo", side_effect=RepoError("no repo")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["run", "--prd", "1"]), 1)

    def test_legacy_config_is_a_startup_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".ralph-gh.config").write_text("RALPH_VERIFY_COMMANDS=()\n")
            repo = RepoContext(Path(tmp), "Me/Repo")
            err = io.StringIO()
            with mock.patch.object(cli, "find_repo", return_value=repo), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["run", "--prd", "1"]), 1)
            self.assertIn("verify_commands", err.getvalue())

    def test_stop_writes_the_stop_file_in_the_repo_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = RepoContext(Path(tmp), "Me/Repo")
            with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": tmp}), mock.patch.object(cli, "find_repo", return_value=repo):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["stop"]), 0)
            self.assertTrue((Path(tmp) / "ralph-gh/state/Me__Repo/STOP").exists())

    def test_run_wires_the_config_and_returns_the_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".ralph-gh.toml").write_text('verify_commands = ["true"]\nparallel = 2\n')
            repo = RepoContext(Path(tmp), "Me/Repo")
            seen = {}

            def fake_run(config, forge, agents, git, **kwargs):  # type: ignore[no-untyped-def]
                seen.update(config=config, kwargs=kwargs)
                return RunResult(3, "cascade")

            with (
                mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": tmp}),
                mock.patch.object(cli, "find_repo", return_value=repo),
                mock.patch.object(cli, "run", fake_run),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = cli.main(["run", "--prd", "52", "--autonomy", "yolo", "--parallel", "5"])
            config = seen["config"]
            self.assertEqual(code, 3)
            self.assertEqual((config.prd, config.parallel, config.autonomy), (52, 5, "yolo"))
            self.assertEqual(config.state_root, Path(tmp) / "ralph-gh/state/Me__Repo")
            self.assertEqual(config.repo_root, Path(tmp))
            self.assertIsNotNone(seen["kwargs"]["stop"])
            self.assertIsNotNone(seen["kwargs"]["env"])
            self.assertIsNotNone(seen["kwargs"]["observer"])


if __name__ == "__main__":
    unittest.main()
