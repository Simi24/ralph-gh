"""AntigravityAgents against a stub `agy` (a Python script): flags, stdin prompt, timeout kill, thread safety, guard env."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from conductor.antigravity_agents import AntigravityAgents, build_command
from conductor.markers import OutcomeKind
from conductor.ports import SessionRequest
from conductor.sessions import SessionRegistry, Stopped

STUB = textwrap.dedent(
    """
    import json, os, signal, subprocess, sys, time
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    if prompt.startswith("SLEEP"):
        child = subprocess.Popen(["sleep", "60"])
        open(sys.argv[0] + ".child", "w").write(str(child.pid))
        time.sleep(60)
    if prompt.startswith("LIMIT"):
        print(json.dumps({"status": "ERROR", "error": "Quota exceeded. Resets at 9am"}))
        sys.exit(1)
    env = {k: v for k, v in os.environ.items() if k.startswith("RALPH_GUARD") or k.startswith("GH_") or k.startswith("GIT_CONFIG_")}
    print(json.dumps({"status": "SUCCESS", "response": json.dumps({"argv": argv, "prompt": prompt, "env": env})}))
    """
)


class AntigravityAgentsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.stub = self.dir / "agy_stub.py"
        self.stub.write_text(STUB)
        self.sessions = SessionRegistry()
        self.agents = AntigravityAgents(self.sessions, (sys.executable, str(self.stub)), kill_grace=1.0)

    def request(self, prompt: str = "hello", **extra: object) -> SessionRequest:
        return SessionRequest(role="implementer", prompt=prompt, cwd=self.dir, **extra)  # type: ignore[arg-type]

    def test_prompt_sent_on_stdin_and_flags(self) -> None:
        result = self.agents.start(self.request("do it", model="gemini-2.5-pro", add_dirs=(self.dir / "a", self.dir / "b")))
        seen = json.loads(result.text)
        self.assertEqual(seen["prompt"], "do it")
        self.assertEqual(
            seen["argv"],
            ["--dangerously-skip-permissions", "--input-format", "text", "--output-format", "json",
             "--add-dir", str(self.dir / "a"), "--add-dir", str(self.dir / "b"),
             "--model", "gemini-2.5-pro"],
        )
        # Not on argv
        self.assertNotIn("--print=do it", seen["argv"])
        self.assertNotIn("-p", seen["argv"])
        self.assertEqual((result.returncode, result.timed_out, result.usage_limit), (0, False, False))

    def test_guard_env_is_applied(self) -> None:
        result = self.agents.start(self.request("test"))
        seen = json.loads(result.text)
        self.assertEqual(seen["env"]["RALPH_GUARD"], "1")
        self.assertEqual(seen["env"]["RALPH_GUARD_ALLOW_MANIFESTS"], "0")
        self.assertEqual(seen["env"]["GH_CONFIG_DIR"], "/var/empty")
        self.assertEqual(seen["env"]["GIT_CONFIG_KEY_0"], "remote.origin.pushurl")

    def test_agent_flag_included(self) -> None:
        command = build_command(("agy",), self.request(agent="my-agent"))
        self.assertIn("--agent", command)
        self.assertEqual(command[command.index("--agent") + 1], "my-agent")

    def test_default_model_applied_when_not_in_request(self) -> None:
        agents = AntigravityAgents(self.sessions, (sys.executable, str(self.stub)), default_model="gemini-default")
        result = agents.start(self.request("test"))
        seen = json.loads(result.text)
        self.assertIn("--model", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--model") + 1], "gemini-default")

    def test_usage_limit_is_reported(self) -> None:
        result = self.agents.start(self.request("LIMIT"))
        self.assertTrue(result.usage_limit)
        self.assertEqual(result.usage_reset, "Resets at 9am")

    def test_timeout_kills_the_whole_process_group(self) -> None:
        started = time.monotonic()
        result = self.agents.start(self.request("SLEEP", timeout=1))
        self.assertTrue(result.timed_out)
        self.assertEqual(result.text, "")
        self.assertLess(time.monotonic() - started, 10)
        pid = int(Path(str(self.stub) + ".child").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_sessions_run_in_their_own_group_and_are_released(self) -> None:
        self.agents.start(self.request())
        self.assertEqual(self.sessions._procs, set())

    def test_threads_do_not_mix_results(self) -> None:
        with ThreadPoolExecutor(6) as pool:
            futures = [pool.submit(self.agents.start, self.request(f"prompt-{i}")) for i in range(12)]
            results = [f.result() for f in futures]
        prompts = [json.loads(r.text)["prompt"] for r in results]
        expected = sorted([f"prompt-{i}" for i in range(12)])
        self.assertEqual(sorted(prompts), expected)


if __name__ == "__main__":
    unittest.main()
