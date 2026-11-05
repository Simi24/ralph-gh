"""OpencodeAgents against a stub `opencode` (a Python script): argv, stdin prompt, session config, timeout kill, guard."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from conductor.markers import OutcomeKind, outcome_of
from conductor.opencode_agents import OpencodeAgents, build_command
from conductor.ports import SessionRequest
from conductor.sessions import SessionRegistry

STUB = textwrap.dedent(
    """
    import json, os, subprocess, sys, time
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    # the guard plugin reports loading at startup, before any model call
    if not prompt.startswith("NOPLUGIN") and os.environ.get("RALPH_GUARD") == "1":
        ready = os.environ.get("RALPH_GUARD_OPENCODE_READY")
        if ready:
            open(ready, "w").write(os.environ.get("RALPH_GUARD_OPENCODE_NONCE", ""))
    if prompt.startswith("SLEEP"):
        child = subprocess.Popen(["sleep", "60"])
        open(sys.argv[0] + ".child", "w").write(str(child.pid))
        time.sleep(60)
    if prompt.startswith("LIMIT"):
        print(json.dumps({"type": "error", "error": {"name": "APIError", "data": {"message": "Rate limit exceeded", "statusCode": 429}}}))
        sys.exit(1)
    if prompt.startswith("FALLBACK"):
        print('agent "x" not found. Falling back to default agent', file=sys.stderr)
    if prompt.startswith("MANIFEST"):
        open("package.json", "w").write("{}")
    keys = ("RALPH_GUARD", "GIT_CONFIG_", "OPENCODE_")
    env = {k: v for k, v in os.environ.items() if k.startswith(keys)}
    seen = json.dumps({"argv": argv, "prompt": prompt, "env": env, "path": os.environ.get("PATH", ""), "pwd": os.environ.get("PWD", "")})
    print(json.dumps({"type": "text", "part": {"type": "text", "messageID": "m1", "text": seen}}))
    """
)


class OpencodeAgentsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.stub = self.dir / "opencode_stub.py"
        self.stub.write_text(STUB)
        self.bundle = self.dir / "bundle"
        (self.bundle / "opencode").mkdir(parents=True)
        (self.bundle / "opencode" / "ralph-guard.ts").write_text("export const RalphGuard = async () => ({})\n")
        (self.bundle / "agents").mkdir()
        (self.bundle / "agents" / "ralph-ticket-gate.md").write_text(
            "---\nname: ralph-ticket-gate\nmodel: sonnet\n---\n\nYou are the light gate.\nEnd with GATE:PASS or GATE:FAIL.\n"
        )
        self.sessions = SessionRegistry()
        self.agents = self.make()

    def make(self, **extra: object) -> OpencodeAgents:
        return OpencodeAgents(self.sessions, (sys.executable, str(self.stub)), kill_grace=1.0, bundle=self.bundle, **extra)  # type: ignore[arg-type]

    def request(self, prompt: str = "hello", **extra: object) -> SessionRequest:
        return SessionRequest(role="implementer", prompt=prompt, cwd=self.dir, **extra)  # type: ignore[arg-type]

    def seen(self, result) -> dict:  # type: ignore[no-untyped-def]
        return json.loads(result.text)

    def config(self, result) -> dict:  # type: ignore[no-untyped-def]
        return json.loads(self.seen(result)["env"]["OPENCODE_CONFIG_CONTENT"])

    def test_prompt_on_stdin_and_argv(self) -> None:
        result = self.agents.start(self.request("do it", model="opencode-go/deepseek-v4.1-flash"))
        seen = self.seen(result)
        self.assertEqual(seen["prompt"], "do it")
        self.assertEqual(seen["argv"], ["run", "--format", "json", "--dir", str(self.dir), "--model", "opencode-go/deepseek-v4.1-flash"])
        self.assertEqual((result.returncode, result.timed_out, result.usage_limit), (0, False, False))

    def test_the_session_runs_in_its_worktree_not_where_the_conductor_runs(self) -> None:
        # OpenCode takes its project directory from PWD, not from the process cwd
        seen = self.seen(self.agents.start(self.request()))
        self.assertEqual(seen["pwd"], str(self.dir))
        self.assertEqual(seen["argv"][seen["argv"].index("--dir") + 1], str(self.dir))

    def test_default_model_when_the_request_has_none(self) -> None:
        seen = self.seen(self.make(default_model="opencode-go/kimi").start(self.request()))
        self.assertEqual(seen["argv"][-2:], ["--model", "opencode-go/kimi"])
        self.assertNotIn("--model", self.seen(self.agents.start(self.request()))["argv"])

    def test_every_session_loads_the_guard_plugin_and_never_shares(self) -> None:
        config = self.config(self.agents.start(self.request()))
        self.assertEqual(config["plugin"], [(self.bundle / "opencode" / "ralph-guard.ts").as_uri()])
        self.assertEqual(config["share"], "disabled")
        self.assertIs(config["autoupdate"], False)
        self.assertNotIn("agent", config)

    def test_guard_env_is_applied(self) -> None:
        seen = self.seen(self.agents.start(self.request()))
        self.assertEqual(seen["env"]["RALPH_GUARD"], "1")
        self.assertEqual(seen["env"]["RALPH_GUARD_ALLOW_MANIFESTS"], "0")
        self.assertEqual(seen["env"]["GIT_CONFIG_KEY_0"], "remote.origin.pushurl")
        self.assertTrue(seen["path"].split(":")[0].endswith("/bin"))

    def test_exploration_gets_its_extra_roots(self) -> None:
        notes = self.dir / "notes"
        seen = self.seen(self.agents.start(SessionRequest(role="exploration", prompt="x", cwd=self.dir, add_dirs=(notes,))))
        self.assertEqual(seen["env"]["RALPH_GUARD_ROOTS"], str(notes))

    def test_a_bundle_agent_becomes_an_opencode_agent_that_cannot_edit(self) -> None:
        result = self.agents.start(SessionRequest(role="ticket-gate", prompt="review", cwd=self.dir, agent="ralph-guard:ralph-ticket-gate"))
        self.assertEqual(self.seen(result)["argv"], ["run", "--format", "json", "--dir", str(self.dir), "--agent", "ralph-ticket-gate"])
        agent = self.config(result)["agent"]["ralph-ticket-gate"]
        self.assertEqual(agent["mode"], "primary")
        self.assertEqual(agent["prompt"], "You are the light gate.\nEnd with GATE:PASS or GATE:FAIL.\n")
        self.assertIs(agent["disable"], False)
        self.assertEqual(agent["permission"], {"edit": "deny", "task": "deny"})

    def test_an_agent_outside_the_bundle_never_starts_a_session(self) -> None:
        for name in ("my-reviewer", "other-plugin:reviewer", "ralph-guard:missing"):
            with self.subTest(name):
                result = self.agents.start(SessionRequest(role="final-review", prompt="review", cwd=self.dir, agent=name))
                self.assertEqual(result.returncode, 1)
                self.assertIn("opencode engine", result.text)
                self.assertNotIn("GATE:PASS", result.text)

    def test_a_session_whose_guard_plugin_did_not_load_is_discarded(self) -> None:
        # OpenCode skips a plugin that fails to load and carries on, exit 0
        result = self.agents.start(self.request("NOPLUGIN\nRALPH:DONE"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not load", result.text)
        self.assertEqual(outcome_of(result).kind, OutcomeKind.UNPARSABLE)

    def test_a_review_that_fell_back_to_another_agent_is_discarded(self) -> None:
        result = self.agents.start(SessionRequest(role="ticket-gate", prompt="FALLBACK", cwd=self.dir, agent="ralph-guard:ralph-ticket-gate"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("fell back", result.text)

    def test_inherited_opencode_switches_are_dropped_and_project_config_is_off(self) -> None:
        os.environ["OPENCODE_PURE"] = "1"
        os.environ["OPENCODE_PERMISSION"] = '{"edit": "allow"}'
        self.addCleanup(os.environ.pop, "OPENCODE_PURE", None)
        self.addCleanup(os.environ.pop, "OPENCODE_PERMISSION", None)
        env = self.seen(self.agents.start(self.request()))["env"]
        self.assertNotIn("OPENCODE_PURE", env)
        self.assertNotIn("OPENCODE_PERMISSION", env)
        self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        self.assertEqual(env["OPENCODE_DISABLE_AUTOUPDATE"], "1")

    def test_the_repo_rules_are_loaded_explicitly(self) -> None:
        # with project config off, OpenCode no longer reads AGENTS.md by itself
        self.assertNotIn("instructions", self.config(self.agents.start(self.request())))
        (self.dir / "CLAUDE.md").write_text("claude rules")
        self.assertEqual(self.config(self.agents.start(self.request()))["instructions"], [str(self.dir / "CLAUDE.md")])
        (self.dir / "AGENTS.md").write_text("rules")
        self.assertEqual(self.config(self.agents.start(self.request()))["instructions"], [str(self.dir / "AGENTS.md")])

    def test_a_missing_guard_plugin_never_starts_a_session(self) -> None:
        (self.bundle / "opencode" / "ralph-guard.ts").unlink()
        result = self.agents.start(self.request("SLEEP"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ralph-guard", result.text)
        self.assertFalse(Path(str(self.stub) + ".child").exists())

    def test_a_manifest_edit_blocks_the_session(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)
        result = self.agents.start(self.request("MANIFEST"))
        self.assertIn("package.json", result.text)
        self.assertEqual(outcome_of(result).kind, OutcomeKind.BLOCKED)

    def test_manifest_edits_pass_when_allowed(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)
        result = self.make(allow_manifest_edits=True).start(self.request("MANIFEST"))
        self.assertEqual(self.seen(result)["env"]["RALPH_GUARD_ALLOW_MANIFESTS"], "1")

    def test_usage_limit_is_reported(self) -> None:
        result = self.agents.start(self.request("LIMIT"))
        self.assertTrue(result.usage_limit)

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

    def test_sessions_are_released(self) -> None:
        self.agents.start(self.request())
        self.assertEqual(self.sessions._procs, set())

    def test_threads_do_not_mix_results(self) -> None:
        with ThreadPoolExecutor(6) as pool:
            futures = [pool.submit(self.agents.start, self.request(f"prompt-{i}")) for i in range(12)]
            prompts = sorted(self.seen(f.result())["prompt"] for f in futures)
        self.assertEqual(prompts, sorted(f"prompt-{i}" for i in range(12)))

    def test_build_command_never_puts_the_prompt_on_argv(self) -> None:
        command = build_command(("opencode",), self.request("secret prompt"), None)
        self.assertNotIn("secret prompt", " ".join(command))


if __name__ == "__main__":
    unittest.main()
