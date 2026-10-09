import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from conductor.config import Config, parse_config
from conductor.preflight import REQUIRED_LABELS, find_agent, preflight


@dataclass
class FakeEnv:
    tools: set[str] = field(default_factory=lambda: {"gh", "git", "claude"})
    authenticated: bool = True
    perms: dict[str, bool] | None = field(default_factory=lambda: {"push": True, "triage": True})
    status: list[str] = field(default_factory=list)
    fetch_ok: bool = True
    labels: set[str] | None = field(default_factory=set)  # None = unreadable
    create_ok: bool = True
    agent_dirs: list[Path] = field(default_factory=list)
    bundle_problem: str | None = None
    shell_rc: int = 0
    health: list[str | None] = field(default_factory=list)  # probe answers in order; None = healthy
    created: list[str] = field(default_factory=list)
    shells: list[str] = field(default_factory=list)
    probes: int = 0
    sleeps: int = 0

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    def gh_authenticated(self) -> bool:
        return self.authenticated

    def permissions(self) -> dict[str, bool] | None:
        return self.perms

    def git_status(self) -> list[str]:
        return self.status

    def fetch_base(self, base: str) -> bool:
        return self.fetch_ok

    def existing_labels(self) -> set[str] | None:
        return self.labels

    def create_label(self, name: str, color: str, description: str) -> bool:
        self.created.append(name)
        return self.create_ok

    def agent_roots(self) -> list[Path]:
        return self.agent_dirs

    def worker_bundle_problem(self) -> str | None:
        return self.bundle_problem

    def run_shell(self, command: str) -> int:
        self.shells.append(command)
        return self.shell_rc

    def probe_health(self, url: str) -> str | None:
        self.probes += 1
        return self.health.pop(0) if self.health else None

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1


def make_config(**extra: object) -> Config:
    return parse_config({"verify_commands": ["true"], "reviewer_agent": "plugin:reviewer", "ticket_gate_agent": "plugin:gate", **extra})


class PreflightTest(unittest.TestCase):
    def failure(self, env: FakeEnv, config: Config | None = None) -> str:
        result = preflight(config or make_config(), env)
        self.assertIsNotNone(result)
        return result  # type: ignore[return-value]

    def test_healthy_environment_passes(self) -> None:
        self.assertIsNone(preflight(make_config(), FakeEnv()))

    def test_each_failed_check_is_named(self) -> None:
        cases = [
            ("tool claude", FakeEnv(tools={"gh", "git"})),
            ("tool gh", FakeEnv(tools={"git", "claude"})),
            ("tool git", FakeEnv(tools={"gh", "claude"})),
            ("gh auth", FakeEnv(authenticated=False)),
            ("permissions", FakeEnv(perms=None)),
            ("permissions", FakeEnv(perms={"push": True, "triage": False})),
            ("permissions", FakeEnv(perms={"push": False, "triage": True})),
            ("clean tree", FakeEnv(status=[" M file.py"])),
            ("fetch", FakeEnv(fetch_ok=False)),
            ("worker bundle", FakeEnv(bundle_problem="not found at /x")),
            ("worker bundle", FakeEnv(bundle_problem="the installed Claude Code cannot load it")),
            ("labels", FakeEnv(labels=None)),
            ("labels", FakeEnv(create_ok=False)),
        ]
        for name, env in cases:
            with self.subTest(name, env=env):
                self.assertIn(name, self.failure(env))
                self.assertEqual((env.shells, env.probes), ([], 0))

    def test_ralph_files_untracked_do_not_dirty_the_tree(self) -> None:
        env = FakeEnv(status=["?? .ralph-gh.toml", "?? .ralph-gh.config", "?? .ralph-gh/"])
        self.assertIsNone(preflight(make_config(), env))

    def test_missing_labels_created_existing_untouched(self) -> None:
        names = {name for name, _, _ in REQUIRED_LABELS}
        env = FakeEnv(labels={"ralph:queued", "other"})
        self.assertIsNone(preflight(make_config(), env))
        self.assertEqual(set(env.created), names - {"ralph:queued"})

    def test_reviewer_agent_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / "agents" / "nested"
            agents.mkdir(parents=True)
            (agents / "file-name-differs.md").write_text('---\nname: "my-reviewer"\n---\nbody\n')
            (agents / "decoy.md").write_text("no frontmatter\nname: other-reviewer\n")
            env = FakeEnv(agent_dirs=[Path(tmp) / "agents", Path(tmp) / "absent"])
            self.assertIsNone(preflight(make_config(reviewer_agent="my-reviewer"), env))
            message = self.failure(env, make_config(reviewer_agent="other-reviewer"))
            self.assertIn("reviewer agent", message)
            self.assertIn("other-reviewer", message)
            self.assertIsNone(find_agent("nope", env.agent_dirs))

    def test_default_agents_are_the_bundles_own(self) -> None:
        bundle_agents = Path(__file__).resolve().parent.parent / "worker-bundle" / "agents"
        config = parse_config({"verify_commands": ["true"]})
        self.assertIsNone(preflight(config, FakeEnv(agent_dirs=[bundle_agents])))
        self.assertIn("reviewer agent", self.failure(FakeEnv(agent_dirs=[]), config))

    def test_ticket_gate_agent_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / "agents"
            agents.mkdir()
            (agents / "gate.md").write_text("---\nname: my-gate\n---\nbody\n")
            env = FakeEnv(agent_dirs=[agents])
            self.assertIsNone(preflight(make_config(ticket_gate_agent="my-gate"), env))
            message = self.failure(env, make_config(ticket_gate_agent="missing-one"))
            self.assertIn("ticket gate agent", message)
            self.assertIn("missing-one", message)

    def test_preflight_command_runs_once_when_not_healthy(self) -> None:
        env = FakeEnv()
        config = make_config(preflight_command="docker compose up -d")
        self.assertIsNone(preflight(config, env))
        self.assertEqual(env.shells, ["docker compose up -d"])

    def test_preflight_command_failure_stops(self) -> None:
        env = FakeEnv(shell_rc=1)
        self.assertIn("preflight command", self.failure(env, make_config(preflight_command="boom")))

    def test_command_skipped_when_endpoint_already_healthy(self) -> None:
        env = FakeEnv()
        config = make_config(preflight_command="up", preflight_health_url="http://x/health")
        self.assertIsNone(preflight(config, env))
        self.assertEqual(env.shells, [])

    def test_health_polled_up_to_retries_then_fails_with_reason(self) -> None:
        env = FakeEnv(health=["timeout"] * 10)
        config = make_config(preflight_health_url="http://x/health", preflight_health_retries=3)
        message = self.failure(env, config)
        self.assertIn("health check", message)
        self.assertIn("http://x/health", message)
        self.assertIn("timeout", message)
        self.assertEqual(env.probes, 3)  # no command configured: exactly `retries` polls

    def test_health_turning_green_passes(self) -> None:
        env = FakeEnv(health=["down", "down", "down", None])
        config = make_config(
            preflight_command="up", preflight_health_url="http://x/health", preflight_health_retries=5
        )
        self.assertIsNone(preflight(config, env))
        self.assertEqual(env.shells, ["up"])
        self.assertEqual(env.probes, 4)


if __name__ == "__main__":
    unittest.main()
