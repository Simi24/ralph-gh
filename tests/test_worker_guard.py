"""Unit tests for ralph-guard execution shims (git and gh) and environment isolation."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from conductor.ports import SessionRequest
from conductor.worker_bundle import bundle_dir, check_manifest_edits, guard_env, is_manifest_file


class WorkerGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bin_dir = bundle_dir() / "bin"
        self.git_shim = self.bin_dir / "git"
        self.gh_shim = self.bin_dir / "gh"
        self.assertTrue(self.git_shim.is_file(), f"missing git shim: {self.git_shim}")
        self.assertTrue(self.gh_shim.is_file(), f"missing gh shim: {self.gh_shim}")

    def test_guard_env_keeps_gh_auth_for_reads_and_configures_isolation(self) -> None:
        base = {
            "PATH": "/usr/bin:/bin",
            "GH_TOKEN": "secret-gh-token",
            "GITHUB_TOKEN": "secret-github-token",
            "GH_ENTERPRISE_TOKEN": "secret-ent-token",
            "GH_HOST": "github.com",
            "OTHER_VAR": "keep-me",
        }
        req = SessionRequest(role="implementer", prompt="", cwd=Path("/tmp"))
        env = guard_env(req, allow_manifest_edits=False, base=base)

        self.assertEqual(env["RALPH_GUARD"], "1")
        self.assertEqual(env["RALPH_GUARD_ALLOW_MANIFESTS"], "0")
        self.assertNotIn("GH_CONFIG_DIR", env)
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "remote.origin.pushurl")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "http://127.0.0.1:0/blocked")
        self.assertEqual(env["GIT_SSH_COMMAND"], "false")

        # gh auth kept: sessions read their ticket and PRD with `gh issue view`
        self.assertEqual(env["GH_TOKEN"], "secret-gh-token")
        self.assertEqual(env["GITHUB_TOKEN"], "secret-github-token")
        self.assertEqual(env["GH_HOST"], "github.com")
        self.assertEqual(env["OTHER_VAR"], "keep-me")

        # PATH has worker-bundle/bin first
        self.assertTrue(env["PATH"].startswith(f"{self.bin_dir}:"))

    def test_git_shim_refuses_push_when_active(self) -> None:
        env = {"RALPH_GUARD": "1", "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}"}
        proc = subprocess.run([sys.executable, str(self.git_shim), "push", "origin", "main"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("only the conductor pushes; commit your work and report RALPH:DONE", proc.stderr)

    def test_git_shim_refuses_push_with_flags(self) -> None:
        env = {"RALPH_GUARD": "1", "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}"}
        proc = subprocess.run([sys.executable, str(self.git_shim), "-C", "/tmp", "push"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("only the conductor pushes; commit your work and report RALPH:DONE", proc.stderr)

    def test_git_shim_refuses_no_verify(self) -> None:
        env = {"RALPH_GUARD": "1", "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}"}
        proc = subprocess.run([sys.executable, str(self.git_shim), "commit", "--no-verify", "-m", "msg"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--no-verify is not allowed", proc.stderr)

    def test_gh_shim_refuses_pr_merge(self) -> None:
        env = {"RALPH_GUARD": "1", "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}"}
        proc = subprocess.run([sys.executable, str(self.gh_shim), "pr", "merge", "123"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("GitHub writes (PRs, issues, labels, comments, merges) belong to the conductor", proc.stderr)

    def test_gh_shim_refuses_pr_create(self) -> None:
        env = {"RALPH_GUARD": "1", "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}"}
        proc = subprocess.run([sys.executable, str(self.gh_shim), "pr", "create", "--title", "test"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("GitHub writes", proc.stderr)

    def test_gh_shim_refuses_issue_create(self) -> None:
        env = {"RALPH_GUARD": "1", "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}"}
        proc = subprocess.run([sys.executable, str(self.gh_shim), "issue", "create", "--title", "test"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("GitHub writes", proc.stderr)

    def test_gh_shim_refuses_api_writes(self) -> None:
        env = {"RALPH_GUARD": "1", "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}"}
        # POST method
        proc = subprocess.run([sys.executable, str(self.gh_shim), "api", "-X", "POST", "repos/o/r/issues"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("gh api writes belong to the conductor", proc.stderr)

        # Field implies write
        proc2 = subprocess.run([sys.executable, str(self.gh_shim), "api", "repos/o/r/issues", "-f", "title=bug"], env=env, capture_output=True, text=True)
        self.assertEqual(proc2.returncode, 1)
        self.assertIn("gh api writes belong to the conductor", proc2.stderr)

    def test_manifest_file_detection(self) -> None:
        self.assertTrue(is_manifest_file("package.json"))
        self.assertTrue(is_manifest_file("pyproject.toml"))
        self.assertTrue(is_manifest_file("requirements.txt"))
        self.assertTrue(is_manifest_file("requirements-dev.txt"))
        self.assertTrue(is_manifest_file("Cargo.lock"))
        self.assertFalse(is_manifest_file("main.py"))
        self.assertFalse(is_manifest_file("README.md"))

    def test_check_manifest_edits_detects_modified_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            (repo / "normal.txt").write_text("hello")
            subprocess.run(["git", "add", "normal.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

            # Clean repo has no manifest edit
            self.assertIsNone(check_manifest_edits(repo))

            # Modify normal file -> still None
            (repo / "normal.txt").write_text("hello world")
            self.assertIsNone(check_manifest_edits(repo))

            # Create manifest file -> detected
            (repo / "package.json").write_text("{}")
            self.assertEqual(check_manifest_edits(repo), "package.json")


if __name__ == "__main__":
    unittest.main()
