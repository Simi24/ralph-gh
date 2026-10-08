"""One conductor per repo: a second one is refused before it touches the board."""
import subprocess
import sys
import unittest

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.git_adapter import GitCli
from conductor.lock import RunLock
from conductor.ports import Issue
from conductor.run import EXIT_OK, EXIT_STARTUP_ERROR, run
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52


class RunLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Lock PRD")])
        self.forge.add_sub_issues(PRD, [Issue(1, "ticket 1", frozenset({"ralph:queued"}))])
        self.agents = FakeAgents(
            {
                "implementer": lambda r: commits_file("a.txt")(r),
                "ticket-gate": says("GATE:PASS"),
            }
        )
        self.config = Config(verify_commands=("true",)).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root
        )
        notes = notes_path(self.config)
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("notes")
        self.lock_file = self.repo.state_root / "lock"

    def start(self):
        return run(self.config, self.forge, self.agents, GitCli(self.repo.checkout))

    def test_a_second_conductor_is_refused_before_it_touches_anything(self) -> None:
        first = RunLock(self.lock_file)
        self.assertTrue(first.acquire())
        self.addCleanup(first.release)
        result = self.start()
        self.assertEqual(result.exit_code, EXIT_STARTUP_ERROR)
        self.assertIn("another conductor", result.reason)
        self.assertEqual(self.forge.writes, 0)  # no label, comment or PR
        self.assertEqual(self.agents.requests, [])
        self.assertFalse((self.repo.state_root / "run.log").exists())

    def test_a_lock_left_by_a_dead_process_is_replaced(self) -> None:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file.write_text(f"{dead.pid}\n")
        result = self.start()
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertIn("ralph:integrated", self.forge.get_issue(1).labels)

    def test_the_lock_is_released_when_the_run_ends(self) -> None:
        self.start()
        again = RunLock(self.lock_file)
        self.assertTrue(again.acquire())
        again.release()

    def test_the_lock_is_released_when_the_run_crashes(self) -> None:
        def boom(request):
            raise RuntimeError("session crashed")

        self.agents.behaviors["implementer"] = boom
        with self.assertRaises(RuntimeError):
            self.start()
        again = RunLock(self.lock_file)
        self.assertTrue(again.acquire())
        again.release()


if __name__ == "__main__":
    unittest.main()
