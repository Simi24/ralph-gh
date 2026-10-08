"""The CLI entry point, driven as a process: real `git` in a temp repo, stub `gh` and `claude`
binaries first on PATH. Nothing inside the package is patched."""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from conductor import cli, labels
from conductor.preflight import REQUIRED_LABELS
from conductor.state_dir import default_state_root
from tests.gitrepo import TempRepo

REPO = Path(__file__).resolve().parent.parent

GH_STUB = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    args = sys.argv[1:]
    if args[:2] == ["auth", "status"]:
        sys.exit(0 if os.environ.get("STUB_AUTH", "1") == "1" else 1)
    if args[:2] == ["repo", "view"]:
        if os.environ.get("STUB_REPO_FAIL"):
            print("no github remote", file=sys.stderr)
            sys.exit(1)
        print("Me/Repo")
    elif args[:2] == ["label", "list"]:
        print(os.environ.get("STUB_LABELS", ""))
    elif args[:1] == ["api"]:
        path = next(a for a in args[1:] if a.startswith("repos/"))
        if "/issues/" not in path:
            print(json.dumps({{"push": True, "triage": True}}))  # the permissions probe
        elif "sub_issues" in path:
            print("[]")
        else:
            print(json.dumps({{"number": 52, "title": "A PRD", "state": "open", "labels": []}}))
    else:
        sys.exit(1)
    """
)


def write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class UsageTest(unittest.TestCase):
    def parse_error(self, *argv: str) -> int:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(list(argv))
        return int(raised.exception.code)  # type: ignore[arg-type]

    def test_usage_errors_exit_2(self) -> None:
        for argv in (["run"], ["run", "--prd", "x"], ["run", "--prd", "0"], ["run", "--prd", "1", "--parallel", "0"],
                     ["run", "--prd", "1", "--autonomy", "nope"], ["bogus"], []):
            with self.subTest(argv):
                self.assertEqual(self.parse_error(*argv), 2)


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


class CliProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        write_executable(self.bin / "gh", GH_STUB.format(python=sys.executable))
        write_executable(self.bin / "claude", "#!/bin/sh\nexit 0\n")
        self.config_dir = self.tmp / "claude-config"
        self.launcher = REPO / "ralph-gh"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_CONFIG_DIR": str(self.config_dir),
            "STUB_LABELS": "\n".join(name for name, _, _ in REQUIRED_LABELS),
        }

    def ralph(self, *argv: str, cwd: Path | None = None, launcher: Path | None = None, **env: str):
        return subprocess.run(
            [sys.executable, str(launcher or self.launcher), *argv], cwd=cwd or self.repo.checkout,
            env={**self.env, **env}, capture_output=True, text=True, timeout=60,
        )

    def write_config(self, extra: str = "") -> None:
        (self.repo.checkout / ".ralph-gh.toml").write_text(
            f'verify_commands = ["true"]\nreviewer_agent = "p:reviewer"\nticket_gate_agent = "p:gate"\n{extra}'
        )

    def test_outside_a_repo_is_a_startup_error(self) -> None:
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        done = self.ralph("run", "--prd", "1", cwd=outside, GIT_CEILING_DIRECTORIES=str(self.tmp))
        self.assertEqual(done.returncode, 1)
        self.assertIn("could not resolve the repository root", done.stderr)

    def test_a_repo_gh_cannot_name_is_a_startup_error(self) -> None:
        done = self.ralph("run", "--prd", "1", STUB_REPO_FAIL="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("could not resolve the GitHub repo", done.stderr)

    def test_legacy_config_is_a_startup_error(self) -> None:
        (self.repo.checkout / ".ralph-gh.config").write_text("RALPH_VERIFY_COMMANDS=()\n")
        done = self.ralph("run", "--prd", "1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("verify_commands", done.stderr)

    def test_an_invalid_config_value_is_a_startup_error_naming_the_key(self) -> None:
        self.write_config('base_branch = "--upload-pack=x"\n')
        done = self.ralph("run", "--prd", "1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("base_branch", done.stderr)

    def test_stop_writes_the_stop_file_in_the_repo_state_dir(self) -> None:
        done = self.ralph("stop")
        self.assertEqual(done.returncode, 0)
        self.assertTrue((self.config_dir / "ralph-gh/state/Me__Repo/STOP").exists())

    def test_run_loads_the_config_passes_preflight_and_reports_the_run_reason(self) -> None:
        self.write_config("parallel = 2\n")
        done = self.ralph("run", "--prd", "52", "--autonomy", "yolo", "--parallel", "5")
        self.assertEqual(done.returncode, 1)
        self.assertIn("ralph-gh: PRD #52 has no sub-issues (exit 1)", done.stderr)  # got past preflight and the board read
        self.assertTrue((self.config_dir / "ralph-gh/state/Me__Repo").is_dir())

    def test_a_failed_preflight_is_a_startup_error_and_runs_nothing(self) -> None:
        self.write_config()
        done = self.ralph("run", "--prd", "52", STUB_AUTH="0")
        self.assertEqual(done.returncode, 1)
        self.assertIn("preflight failed: gh auth", done.stderr)

    def test_labels_that_cannot_be_created_fail_preflight(self) -> None:
        self.write_config()
        done = self.ralph("run", "--prd", "52", STUB_LABELS="")  # none exist and the stub cannot create any
        self.assertEqual(done.returncode, 1)
        self.assertIn("preflight failed: labels", done.stderr)

    def test_run_warns_when_the_installed_copy_is_behind_but_still_runs(self) -> None:
        self.write_config()
        installed = self.tmp / "installed"
        installed.mkdir()
        shutil.copytree(REPO / "conductor", installed / "conductor", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(self.launcher, installed / "ralph-gh")
        (installed / ".installed").write_text(f"source_path={self.repo.checkout}\nsource_sha={'a' * 40}\n")
        done = self.ralph("run", "--prd", "52", launcher=installed / "ralph-gh")
        self.assertIn("WARNING: installed copy is behind your clone", done.stderr)
        self.assertIn("PRD #52 has no sub-issues", done.stderr)  # advisory only: the run went on


if __name__ == "__main__":
    unittest.main()
