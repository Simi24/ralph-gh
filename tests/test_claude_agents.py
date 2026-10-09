"""ClaudeAgents against a stub `claude` (a Python script): flags, stdin, timeout kill, thread safety."""
import json
import os
import signal
import sys
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from conductor.claude_agents import ClaudeAgents, build_command
from conductor.ports import SessionRequest
from conductor.sessions import SessionRegistry, Stopped

STUB = textwrap.dedent(
    """
    import json, os, signal, subprocess, sys, time
    prompt = sys.stdin.read()
    if prompt.startswith(("HANG", "STUBBORN")):  # a live session an immediate stop must kill
        if prompt.startswith("STUBBORN"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(["sleep", "60"])
        open(sys.argv[0] + ".child", "w").write(str(child.pid))
        open(sys.argv[0] + ".ready", "w").write(str(os.getpgid(0)))
        time.sleep(60)
    if prompt.startswith("SLEEP"):  # a hung session with a grandchild in the same group
        child = subprocess.Popen(["sleep", "60"])
        open(sys.argv[0] + ".child", "w").write(str(child.pid))
        time.sleep(60)
    if prompt.startswith("LIMIT"):
        print(json.dumps({"is_error": True, "result": "You've hit your session limit. resets at 9am"}))
        sys.exit(1)
    guard = {k: v for k, v in os.environ.items() if k.startswith("RALPH_GUARD")}
    print(json.dumps({"is_error": False, "result": json.dumps({"argv": sys.argv[1:], "prompt": prompt, "guard": guard})}))
    """
)


class ClaudeAgentsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.stub = self.dir / "claude_stub.py"
        self.stub.write_text(STUB)
        self.sessions = SessionRegistry()
        self.bundle = self.dir / "bundle"
        self.agents = ClaudeAgents(self.sessions, (sys.executable, str(self.stub)), kill_grace=1.0, bundle=self.bundle)

    def request(self, prompt: str = "hello", **extra: object) -> SessionRequest:
        return SessionRequest(role="implementer", prompt=prompt, cwd=self.dir, **extra)  # type: ignore[arg-type]

    def test_prompt_on_stdin_and_flags(self) -> None:
        result = self.agents.start(self.request("do it", model="m1", add_dirs=(self.dir / "a", self.dir / "b")))
        seen = json.loads(result.text)
        self.assertEqual(seen["prompt"], "do it")
        self.assertEqual(
            seen["argv"],
            ["--dangerously-skip-permissions", "--print", "--output-format", "json",
             "--plugin-dir", str(self.bundle), "--add-dir", str(self.dir / "a"), "--add-dir", str(self.dir / "b"), "--model", "m1"],
        )
        self.assertEqual((result.returncode, result.timed_out, result.usage_limit), (0, False, False))

    def test_reviewer_gets_agent_and_no_model(self) -> None:
        command = build_command(("claude",), self.request(agent="ralph-guard:ralph-gate-reviewer"), self.bundle)
        self.assertIn("--agent", command)
        self.assertNotIn("--model", command)
        self.assertEqual(command[0], "claude")

    def seen_by(self, role: str, agents: ClaudeAgents | None = None, **extra: object) -> dict:
        request = SessionRequest(role=role, prompt="go", cwd=self.dir, **extra)  # type: ignore[arg-type]
        return json.loads((agents or self.agents).start(request).text)

    def test_every_worker_role_loads_the_bundle_with_the_guard_on_and_only_exploration_gets_the_notes_root(self) -> None:
        notes = (self.dir / "notes",)
        for role in ("exploration", "implementer", "fix", "merge-fix", "integration-fix", "final-fix", "ticket-gate", "final-review"):
            with self.subTest(role):
                seen = self.seen_by(role, add_dirs=notes)
                self.assertEqual(seen["argv"][seen["argv"].index("--plugin-dir") + 1], str(self.bundle))
                self.assertEqual(seen["guard"]["RALPH_GUARD"], "1")
                self.assertEqual(seen["guard"]["RALPH_GUARD_ALLOW_MANIFESTS"], "0")
                if role == "exploration":
                    self.assertEqual(seen["guard"]["RALPH_GUARD_ROOTS"], str(notes[0]))
                else:
                    self.assertNotIn("RALPH_GUARD_ROOTS", seen["guard"])

    def test_allow_manifest_edits_reaches_the_guard(self) -> None:
        agents = ClaudeAgents(self.sessions, (sys.executable, str(self.stub)), bundle=self.bundle, allow_manifest_edits=True)
        self.assertEqual(self.seen_by("implementer", agents)["guard"]["RALPH_GUARD_ALLOW_MANIFESTS"], "1")

    def test_guard_switches_inherited_from_the_operator_never_leak_into_sessions(self) -> None:
        with mock.patch.dict(os.environ, {"RALPH_GUARD_ROOTS": "/home/me", "RALPH_GUARD_ALLOW_MANIFESTS": "1"}):
            guard = self.seen_by("implementer")["guard"]
        self.assertEqual(guard, {"RALPH_GUARD": "1", "RALPH_GUARD_ALLOW_MANIFESTS": "0"})

    def test_usage_limit_is_reported(self) -> None:
        result = self.agents.start(self.request("LIMIT"))
        self.assertTrue(result.usage_limit)
        self.assertEqual(result.usage_reset, "resets at 9am")

    def test_timeout_kills_the_whole_process_group(self) -> None:
        started = time.monotonic()
        result = self.agents.start(self.request("SLEEP", timeout=1))
        self.assertTrue(result.timed_out)
        self.assertEqual(result.text, "")
        self.assertLess(time.monotonic() - started, 10)
        pid = int(Path(str(self.stub) + ".child").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)  # the grandchild died with its group

    def test_sessions_run_in_their_own_group_and_are_released(self) -> None:
        self.agents.start(self.request())
        self.assertEqual(self.sessions._procs, set())  # noqa: SLF001 - registry bookkeeping

    def test_threads_do_not_mix_results(self) -> None:
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda n: self.agents.start(self.request(f"p{n}")), range(12)))
        self.assertEqual([json.loads(r.text)["prompt"] for r in results], [f"p{n}" for n in range(12)])

    def stop_a_live_session(self, prompt: str, sessions: SessionRegistry):
        """Start a session that hangs, wait until it is up, kill every session group, return its result."""
        agents = ClaudeAgents(sessions, (sys.executable, str(self.stub)), kill_grace=1.0, bundle=self.bundle)
        ready = Path(str(self.stub) + ".ready")
        ready.unlink(missing_ok=True)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(agents.start, self.request(prompt))
            deadline = time.monotonic() + 20
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)  # waiting for an external process to come up: there is no event to join
            self.assertTrue(ready.exists())
            self.assertNotEqual(int(ready.read_text()), os.getpgrp())  # its own process group
            sessions.kill_all()
            return future.result(timeout=30)

    def test_an_immediate_stop_kills_a_live_session_and_its_grandchildren_with_sigterm(self) -> None:
        result = self.stop_a_live_session("HANG", SessionRegistry(grace=30))
        self.assertEqual(result.returncode, -signal.SIGTERM)
        pid = int(Path(str(self.stub) + ".child").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_a_session_that_ignores_sigterm_is_sigkilled_after_the_grace_period(self) -> None:
        result = self.stop_a_live_session("STUBBORN", SessionRegistry(grace=0.2))
        self.assertEqual(result.returncode, -signal.SIGKILL)

    def test_registry_run_commands_are_killable_and_a_closed_registry_refuses_them(self) -> None:
        registry = SessionRegistry()
        done = registry.run("exit 3", shell=True)
        self.assertEqual(done.returncode, 3)
        registry.kill_all()  # nothing left running: it must not touch finished sessions
        with self.assertRaises(Stopped):
            registry.run("true", shell=True)

    def test_after_an_immediate_stop_nothing_starts(self) -> None:
        self.sessions.kill_all()
        with self.assertRaises(Stopped):
            self.agents.start(self.request())

    def test_missing_binary_is_not_swallowed(self) -> None:
        agents = ClaudeAgents(self.sessions, (str(self.dir / "nope"),))
        with self.assertRaises(FileNotFoundError):
            agents.start(self.request())


if __name__ == "__main__":
    unittest.main()
