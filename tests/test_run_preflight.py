import tempfile
import unittest
from pathlib import Path

from conductor.config import parse_config
from conductor.ports import Issue
from conductor.run import EXIT_STARTUP_ERROR, run
from tests.fakes import FakeAgents, FakeForge
from tests.test_preflight import FakeEnv


class RunPreflightTest(unittest.TestCase):
    def test_failed_check_exits_before_any_session_or_board_change(self) -> None:
        forge = FakeForge([Issue(52, "PRD")])
        forge.add_sub_issues(52, [Issue(54, "t", frozenset({"ralph:queued"}))])
        agents = FakeAgents({})
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        config = parse_config({"verify_commands": ["true"]}).with_run(
            prd=52, repo_root=Path("."), state_root=Path(state.name)
        )

        result = run(config, forge, agents, git=None, env=FakeEnv(status=[" M x"]))  # type: ignore[arg-type]

        self.assertEqual(result.exit_code, EXIT_STARTUP_ERROR)
        self.assertIn("clean tree", result.reason)
        self.assertEqual(agents.requests, [])
        self.assertEqual(forge.label_trail, {})


if __name__ == "__main__":
    unittest.main()
