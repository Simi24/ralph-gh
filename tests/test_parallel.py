"""Parallel frontier: sessions run in worker threads, the forge is written by
the conductor thread only. Sessions block on events and barriers (with a
timeout that only fires when the scenario is broken), never on sleeps."""
import threading
import unittest

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.git_adapter import GitCli
from conductor.observer import Observer
from conductor.ports import Blocker, Issue
from conductor.run import EXIT_OK, run
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
Q = frozenset({"ralph:queued"})
WAIT = 20  # seconds; a safety net for a broken scenario, never a pacing delay


def ticket(number: int) -> Issue:
    return Issue(number, f"ticket {number}", Q)


def number_of(request) -> int:
    return int(request.cwd.name.removeprefix("ticket-"))


class ParallelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Parallel PRD")])
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.implementer_threads: set[int] = set()
        self.worktrees: dict[int, object] = {}
        self.behaviors = {
            "implementer": self.implement,
            "ticket-gate": says("GATE:PASS"),
            "final-review": says("GATE:PASS"),
            "fix": says("RALPH:DONE"),
        }
        self.during = lambda request: None  # scenario hook, runs inside the implementer

    def implement(self, request):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.worktrees[number_of(request)] = request.cwd
            self.implementer_threads.add(threading.get_ident())
        try:
            self.during(request)
        finally:
            with self.lock:
                self.running -= 1
        return commits_file(f"{request.cwd.name}.txt")(request)

    def start(self, *tickets: Issue, parallel: int, observer: bool = False):
        self.forge.add_sub_issues(PRD, list(tickets))
        self.agents = FakeAgents(self.behaviors)
        config = Config(verify_commands=("true",), parallel=parallel).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root, autonomy="halt-each-pr"
        )
        notes = notes_path(config)
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("notes")
        obs = None
        if observer:
            obs = Observer(self.forge, prd=PRD, state_root=self.repo.state_root, repo_root=self.repo.checkout)
        return run(config, self.forge, self.agents, GitCli(self.repo.checkout), observer=obs)

    def labels(self, number: int) -> frozenset[str]:
        return self.forge.get_issue(number).labels

    def test_three_independent_tickets_run_at_once_in_three_worktrees(self) -> None:
        barrier = threading.Barrier(3, timeout=WAIT)  # breaks unless all three are inside together
        self.during = lambda request: barrier.wait()
        result = self.start(ticket(1), ticket(2), ticket(3), parallel=3)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(len(set(self.worktrees.values())), 3)
        self.assertEqual(self.peak, 3)
        for path in self.worktrees.values():
            self.assertFalse(path.exists())  # removed once the ticket was integrated
        for n in (1, 2, 3):
            self.assertIn("ralph:integrated", self.labels(n))

    def test_parallel_one_works_one_ticket_at_a_time_to_the_same_board_state(self) -> None:
        result = self.start(ticket(1), ticket(2), ticket(3), parallel=1)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.peak, 1)
        for n in (1, 2, 3):
            self.assertIn("ralph:integrated", self.labels(n))
            self.assertNotIn("ralph:queued", self.labels(n))

    def test_a_ticket_is_never_given_to_two_sessions(self) -> None:
        self.start(ticket(1), ticket(2), ticket(3), ticket(4), parallel=3)
        started = [number_of(r) for r in self.agents.requests if r.role == "implementer"]
        self.assertEqual(sorted(started), [1, 2, 3, 4])

    def test_a_merge_dispatches_the_unblocked_ticket_while_others_are_still_running(self) -> None:
        # 3 is independent and stays inside its session until 2 has started. 2 is
        # blocked by 1, so it can only start if the conductor dispatches it
        # right after 1 integrates, without waiting for 3.
        self.forge.blockers = {2: [Blocker("", 1, "open")]}
        two_started = threading.Event()

        def during(request) -> None:
            n = number_of(request)
            if n == 2:
                two_started.set()
            if n == 3:
                if not two_started.wait(WAIT):
                    raise AssertionError("ticket 2 was not dispatched while ticket 3 was running")

        self.during = during
        result = self.start(ticket(1), ticket(2), ticket(3), parallel=3)
        self.assertEqual(result.exit_code, EXIT_OK)
        for n in (1, 2, 3):
            self.assertIn("ralph:integrated", self.labels(n))

    def test_only_the_conductor_thread_writes_to_the_forge(self) -> None:
        barrier = threading.Barrier(3, timeout=WAIT)
        self.during = lambda request: barrier.wait()
        self.start(ticket(1), ticket(2), ticket(3), parallel=3, observer=True)
        self.assertNotIn(threading.get_ident(), self.implementer_threads)  # sessions ran in workers
        self.assertEqual(self.forge.write_threads, {threading.get_ident()})
        self.assertTrue(self.forge.comments.get(PRD))  # the status comment was still written


if __name__ == "__main__":
    unittest.main()
