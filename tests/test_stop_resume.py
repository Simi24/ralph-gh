"""Stop and resume through run(): drain, immediate stop, and a later run that
continues from the board. Scenarios are driven from inside the fake sessions
with events and barriers, never with sleeps."""
import signal
import threading
import unittest

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.git_adapter import GitCli
from conductor.observer import Observer
from conductor.ports import Issue, SessionResult
from conductor.run import EXIT_OK, run
from conductor.stopping import StopState
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
INTEGRATION = "feat/52-stop-prd"
Q = frozenset({"ralph:queued"})
WAIT = 20  # a safety net for a broken scenario, never a pacing delay


def ticket(number: int) -> Issue:
    return Issue(number, f"ticket {number}", Q)


def number_of(request) -> int:
    return int(request.cwd.name.removeprefix("ticket-"))


class StopResumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Stop PRD")])
        self.config = lambda parallel: Config(verify_commands=("true",), parallel=parallel).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root, autonomy="halt-each-pr"
        )
        notes = notes_path(self.config(1))
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("notes")
        self.stop_file = self.repo.state_root / "STOP"
        self.stop = StopState(self.stop_file)
        self.implemented: list[int] = []
        self.flag_when_requeued: list[bool] = []
        real = self.forge.set_labels

        def spy(number: int, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> None:
            if "ralph:queued" in add:
                self.flag_when_requeued.append(self.stop.immediate)
            real(number, add=add, remove=remove)

        self.forge.set_labels = spy  # type: ignore[method-assign]

    def go(self, *tickets: Issue, parallel: int = 1, implementer=None, gate=None, stop=None, observer=None):
        if tickets:
            self.forge.add_sub_issues(PRD, list(tickets))
        agents = FakeAgents(
            {
                "implementer": implementer or self.implement,
                "ticket-gate": gate or says("GATE:PASS"),
                "final-review": says("GATE:PASS"),
                "fix": says("RALPH:DONE"),
            }
        )
        self.agents = agents
        return run(self.config(parallel), self.forge, agents, GitCli(self.repo.checkout), observer=observer,
                   stop=stop or self.stop)

    def implement(self, request):
        self.implemented.append(number_of(request))
        return commits_file(f"{request.cwd.name}.txt")(request)

    def labels(self, number: int) -> frozenset[str]:
        return self.forge.get_issue(number).labels

    def worktree(self, number: int):
        return self.repo.state_root / f"prd-{PRD}" / "worktrees" / f"ticket-{number}"

    def final_review_ran(self) -> bool:
        return any(r.role == "final-review" for r in self.agents.requests)

    # --- drain ---

    def test_a_stop_file_drains_no_new_dispatch_and_the_in_flight_ticket_completes(self) -> None:
        def implementer(request):
            self.stop_file.write_text("")  # `ralph-gh stop`, while ticket 1 is in flight
            return self.implement(request)

        result = self.go(ticket(1), ticket(2), ticket(3), implementer=implementer)
        self.assertEqual((result.exit_code, result.reason), (EXIT_OK, "stopped by operator"))
        self.assertIn("ralph:integrated", self.labels(1))  # its pipeline ran to the merge
        self.assertEqual(self.labels(2), Q)
        self.assertEqual(self.labels(3), Q)
        self.assertEqual(self.implemented, [1])
        self.assertFalse(self.stop_file.exists())  # consumed
        self.assertFalse(self.final_review_ran())

    def test_every_ticket_in_flight_at_the_drain_finishes(self) -> None:
        barrier = threading.Barrier(2, timeout=WAIT)

        def implementer(request):
            barrier.wait()  # both are in flight
            self.stop_file.write_text("")
            return self.implement(request)

        result = self.go(ticket(1), ticket(2), ticket(3), parallel=2, implementer=implementer)
        self.assertEqual(result.reason, "stopped by operator")
        for n in (1, 2):
            self.assertIn("ralph:integrated", self.labels(n))
        self.assertEqual(self.labels(3), Q)

    def test_a_first_sigint_drains_like_the_stop_file(self) -> None:
        def implementer(request):
            self.stop.escalate()  # what the SIGINT handler does the first time
            return self.implement(request)

        result = self.go(ticket(1), ticket(2), implementer=implementer)
        self.assertEqual(result.reason, "stopped by operator")
        self.assertIn("ralph:integrated", self.labels(1))
        self.assertEqual(self.labels(2), Q)
        self.assertFalse(self.stop.immediate)

    def test_a_stop_file_left_by_an_earlier_run_does_not_stop_the_new_one(self) -> None:
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text("")
        result = self.go(ticket(1), ticket(2))
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertNotEqual(result.reason, "stopped by operator")
        self.assertEqual(self.implemented, [1, 2])
        self.assertFalse(self.stop_file.exists())

    # --- immediate stop ---

    def test_an_immediate_stop_kills_sessions_requeues_tickets_without_a_pr_and_removes_worktrees(self) -> None:
        sleeper_started = threading.Event()
        sleepers = []

        def implementer(request):
            if number_of(request) == 1:
                if not sleeper_started.wait(WAIT):
                    raise AssertionError("ticket 2 never started its session")
                self.stop.request_immediate()  # the second SIGINT
                return SessionResult(text="RALPH:DONE")
            proc = self.stop.sessions.spawn(["sleep", "600"])  # a real session process group
            sleepers.append(proc)
            sleeper_started.set()
            return SessionResult(returncode=proc.wait())

        observer = Observer(self.forge, prd=PRD, state_root=self.repo.state_root, repo_root=self.repo.checkout)
        result = self.go(ticket(1), ticket(2), parallel=2, implementer=implementer, observer=observer)

        self.assertEqual((result.exit_code, result.reason), (EXIT_OK, "stopped by operator (immediate)"))
        self.assertEqual(sleepers[0].returncode, -signal.SIGTERM)  # the group was killed
        for n in (1, 2):
            self.assertEqual(self.labels(n), Q)
            self.assertTrue(self.forge.comments[n])
            self.assertFalse(self.worktree(n).exists())
        self.assertEqual(self.flag_when_requeued, [True, True])  # flag first, labels after
        self.assertEqual(self.forge.prs, {})
        self.assertFalse(self.final_review_ran())
        last_run = (self.repo.state_root / "last-run.md").read_text()
        self.assertTrue(last_run.rstrip().endswith("Exit reason: stopped by operator (immediate)"))

    def test_a_ticket_with_an_open_pr_stays_in_review_and_the_other_goes_back_to_the_queue(self) -> None:
        two_in_flight = threading.Event()
        released = threading.Event()

        def implementer(request):
            self.implemented.append(number_of(request))
            if number_of(request) == 2:
                two_in_flight.set()
                if not released.wait(WAIT):
                    raise AssertionError("the stop never came")
            return commits_file(f"{request.cwd.name}.txt")(request)

        def gate(request):  # ticket 1 has its PR by now
            if not two_in_flight.wait(WAIT):
                raise AssertionError("ticket 2 never started")
            self.stop.request_immediate()
            released.set()
            return SessionResult(text="GATE:PASS")

        result = self.go(ticket(1), ticket(2), parallel=2, implementer=implementer, gate=gate)
        self.assertEqual(result.reason, "stopped by operator (immediate)")
        self.assertEqual(self.labels(1), frozenset({"ralph:in-review"}))  # not requeued, not merged
        self.assertEqual(self.labels(2), Q)
        self.assertEqual([p["state"] for p in self.forge.prs.values()], ["open"])
        for n in (1, 2):
            self.assertFalse(self.worktree(n).exists())
        self.assertEqual(self.forge.merges, [])

    def test_a_session_killed_by_the_stop_never_marks_its_ticket_failed(self) -> None:
        def implementer(request):
            self.stop.request_immediate()
            return SessionResult(returncode=-signal.SIGTERM)  # what a killed session returns

        self.go(ticket(1), implementer=implementer)
        self.assertEqual(self.labels(1), Q)
        self.assertNotIn("ralph:failed:issue", self.labels(1))

    def test_an_immediate_stop_during_the_final_review_blocks_nothing(self) -> None:
        self.forge.add_sub_issues(PRD, [Issue(1, "ticket 1", frozenset({"ralph:integrated"}))])

        def final(request):
            self.stop.request_immediate()
            return SessionResult(returncode=-signal.SIGTERM)

        agents = FakeAgents({"final-review": final})
        result = run(self.config(1), self.forge, agents, GitCli(self.repo.checkout), stop=self.stop)
        self.assertEqual(result.reason, "stopped by operator (immediate)")
        self.assertNotIn("ralph:blocked", self.forge.get_issue(PRD).labels)
        self.assertEqual(self.forge.merges, [])

    # --- resume ---

    def test_a_run_after_a_stop_reuses_the_branch_and_pr_and_continues(self) -> None:
        two_in_flight = threading.Event()
        released = threading.Event()

        def implementer(request):
            self.implemented.append(number_of(request))
            if number_of(request) == 2:
                two_in_flight.set()
                released.wait(WAIT)
            return commits_file(f"{request.cwd.name}.txt")(request)

        def gate(request):
            two_in_flight.wait(WAIT)
            self.stop.request_immediate()
            released.set()
            return SessionResult(text="GATE:PASS")

        self.go(ticket(1), ticket(2), parallel=2, implementer=implementer, gate=gate)
        self.repo.git("fetch", "origin")
        integration_tip = self.repo.git("rev-parse", f"origin/{INTEGRATION}")
        (first_pr,) = self.forge.prs
        self.implemented.clear()

        result = self.go(stop=StopState(self.stop_file), parallel=2)  # a fresh process, the same board

        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.implemented, [2])  # ticket 1 was NOT implemented again
        self.assertEqual(self.forge.prs[first_pr]["state"], "merged")  # its existing PR was reused
        self.assertEqual(
            [p["head"] for p in self.forge.prs.values() if p["base"] == INTEGRATION],
            ["feat/52-ticket-1", "feat/52-ticket-2"],  # no second PR for ticket 1
        )
        for n in (1, 2):
            self.assertIn("ralph:integrated", self.labels(n))
        self.repo.git("fetch", "origin")
        self.repo.git("merge-base", "--is-ancestor", integration_tip, f"origin/{INTEGRATION}")  # same branch, grown
        self.assertTrue(self.final_review_ran())

    def test_a_pr_merged_or_closed_by_a_human_while_no_conductor_ran_is_reconciled(self) -> None:
        def pr(n: int, state: str) -> None:
            self.forge.prs[100 + n] = {"head": f"feat/52-ticket-{n}", "base": INTEGRATION, "state": state}

        pr(1, "merged")
        pr(2, "closed")
        in_review = frozenset({"ralph:in-review"})
        result = self.go(
            Issue(1, "merged by a human", in_review),
            Issue(2, "closed by a human", in_review),
            Issue(3, "crashed run", frozenset({"ralph:in-progress"})),
        )
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.forge.ralph_trail(1), [["ralph:in-review"], ["ralph:integrated"]])
        self.assertEqual(sorted(self.implemented), [2, 3])  # back to the queue, then worked again
        for n in (1, 2, 3):
            self.assertIn("ralph:integrated", self.labels(n))


if __name__ == "__main__":
    unittest.main()
