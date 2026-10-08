"""Usage limits and session timeouts, through run(). Time is an injected sleep
function that only records; nothing here really waits."""
import threading
import unittest
from collections.abc import Callable
from dataclasses import replace

from conductor.config import Config, ConfigError, parse_config
from conductor.exploration import notes_path
from conductor.git_adapter import GitCli
from conductor.ports import Issue, SessionRequest, SessionResult
from conductor.run import EXIT_INCOMPLETE, EXIT_OK, run
from conductor.usage_limit import UNKNOWN_RESET
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
Q = frozenset({"ralph:queued"})
WAIT = 20  # seconds; a safety net for a broken scenario, never a pacing delay
LIMITED = SessionResult(text="You've hit your limit", usage_limit=True, usage_reset="resets 5pm")


def ticket(number: int) -> Issue:
    return Issue(number, f"ticket {number}", Q)


def number_of(request: SessionRequest) -> int:
    return int(request.cwd.name.removeprefix("ticket-"))


class Scenario(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Limits PRD")])
        self.sleeps: list[float] = []
        self.behaviors: dict[str, Callable[[SessionRequest], SessionResult]] = {
            "implementer": lambda r: commits_file(f"{r.cwd.name}.txt")(r),
            "ticket-gate": says("GATE:PASS"),
            "final-review": says("GATE:PASS"),
            "fix": says("RALPH:DONE"),
            "exploration": says("RALPH:DONE"),
        }

    def go(self, *tickets: Issue, parallel: int = 1, explored: bool = True, **overrides):
        self.forge.add_sub_issues(PRD, list(tickets))
        self.agents = FakeAgents(self.behaviors)
        config = Config(verify_commands=("true",), parallel=parallel, **overrides).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root, autonomy="halt-each-pr"
        )
        if explored:
            notes = notes_path(config)
            notes.parent.mkdir(parents=True, exist_ok=True)
            notes.write_text("notes")
        return run(config, self.forge, self.agents, GitCli(self.repo.checkout), sleep=self.sleeps.append)

    def labels(self, number: int) -> frozenset[str]:
        return self.forge.get_issue(number).labels

    def assert_nothing_failed(self) -> None:
        for number in self.forge.issues:
            self.assertFalse([l for l in self.labels(number) if l.startswith("ralph:failed")], number)



class UsageLimitTest(Scenario):
    def test_limit_wording_in_a_successful_reply_is_not_a_usage_limit(self) -> None:
        # The adapter reports usage_limit only when is_error is true; a normal reply that
        # merely talks about limits arrives with usage_limit False and is ordinary work.
        self.behaviors["implementer"] = lambda r: commits_file("a.txt", "You've hit your limit, said the doc\nRALPH:DONE")(r)
        result = self.go(ticket(1))
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.sleeps, [])

    def test_a_hit_in_the_implementer_requeues_the_ticket_and_exits(self) -> None:
        self.behaviors["implementer"] = lambda r: LIMITED
        result = self.go(ticket(1))
        self.assertEqual((result.exit_code, result.reason), (EXIT_INCOMPLETE, "usage limit — resets 5pm"))
        self.assertEqual(self.labels(1), Q)
        self.assert_nothing_failed()
        self.assertEqual(self.forge.prs, {})
        self.assertEqual(self.repo.git("worktree", "list").count("\n"), 0)

    def test_an_unstated_reset_reads_as_unknown(self) -> None:
        self.behaviors["implementer"] = lambda r: SessionResult(usage_limit=True)
        result = self.go(ticket(1))
        self.assertEqual(result.reason, f"usage limit — {UNKNOWN_RESET}")

    def test_a_hit_in_the_gate_leaves_a_ticket_with_a_pr_in_review(self) -> None:
        self.behaviors["ticket-gate"] = lambda r: LIMITED
        result = self.go(ticket(1))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.labels(1), frozenset({"ralph:in-review"}))
        self.assertEqual(self.forge.merges, [])
        self.assert_nothing_failed()

    def test_a_hit_in_a_fix_session_is_never_a_failure(self) -> None:
        self.behaviors["ticket-gate"] = says("### Blocking findings\n- x\nGATE:FAIL")
        self.behaviors["fix"] = lambda r: LIMITED
        result = self.go(ticket(1))
        self.assertEqual(result.reason, "usage limit — resets 5pm")
        self.assertEqual(self.labels(1), frozenset({"ralph:in-review"}))
        self.assert_nothing_failed()

    def test_a_hit_stops_dispatching_and_parks_every_in_flight_ticket(self) -> None:
        both_running = threading.Barrier(2, timeout=WAIT)

        def implement(request: SessionRequest) -> SessionResult:
            both_running.wait()
            return LIMITED if number_of(request) == 1 else commits_file("two.txt")(request)

        self.behaviors["implementer"] = implement
        result = self.go(ticket(1), ticket(2), ticket(3), parallel=2)
        self.assertEqual(result.reason, "usage limit — resets 5pm")
        self.assertEqual([self.labels(n) for n in (1, 2, 3)], [Q, Q, Q])  # 2 had no PR yet; 3 never started
        self.assertEqual([r.role for r in self.agents.requests].count("implementer"), 2)
        self.assert_nothing_failed()

    def test_with_wait_for_reset_the_run_waits_and_resumes(self) -> None:
        calls: list[int] = []

        def implement(request: SessionRequest) -> SessionResult:
            calls.append(1)
            return LIMITED if len(calls) == 1 else commits_file("a.txt")(request)

        self.behaviors["implementer"] = implement
        result = self.go(ticket(1), wait_for_reset=True, usage_wait_seconds=12)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(sum(self.sleeps), 12)  # the whole wait, in slices
        self.assertGreater(len(self.sleeps), 1)
        self.assertEqual(self.labels(1), frozenset({"ralph:integrated"}))
        self.assertEqual(len(calls), 2)

    def test_a_default_run_does_not_sleep(self) -> None:
        self.behaviors["implementer"] = lambda r: LIMITED
        self.go(ticket(1))
        self.assertEqual(self.sleeps, [])

    def test_a_hit_during_exploration_pauses_the_run_and_keeps_no_notes(self) -> None:
        self.behaviors["exploration"] = lambda r: LIMITED
        result = self.go(ticket(1), explored=False)
        self.assertEqual(result.reason, "usage limit — resets 5pm")
        self.assertEqual(self.labels(1), Q)
        self.assertEqual([r.role for r in self.agents.requests], ["exploration"])

    def test_a_hit_in_the_final_review_waits_and_reviews_again(self) -> None:
        reviews: list[int] = []

        def review(request: SessionRequest) -> SessionResult:
            reviews.append(1)
            return LIMITED if len(reviews) == 1 else SessionResult(text="GATE:PASS")

        self.behaviors["final-review"] = review
        result = self.go(ticket(1), wait_for_reset=True, usage_wait_seconds=5)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(self.sleeps, [5])
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_a_hit_in_the_final_review_exits_without_waiting_by_default(self) -> None:
        self.behaviors["final-review"] = lambda r: LIMITED
        result = self.go(ticket(1))
        self.assertEqual((result.exit_code, result.reason), (EXIT_INCOMPLETE, "usage limit — resets 5pm"))
        self.assertNotIn("ralph:blocked", self.labels(PRD))


class SessionTimeoutTest(Scenario):
    """The adapter kills a session after `session_timeout` and reports timed_out."""

    def test_every_session_request_carries_the_configured_timeout(self) -> None:
        self.go(ticket(1), session_timeout=99)
        self.assertEqual({r.timeout for r in self.agents.requests}, {99})

    def test_a_timed_out_implementer_fails_the_ticket_without_a_pr(self) -> None:
        self.behaviors["implementer"] = lambda r: SessionResult(text="RALPH:DONE", timed_out=True, returncode=1)
        result = self.go(ticket(1))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertIn("ralph:failed:issue", self.labels(1))
        self.assertEqual(self.forge.merges, [])

    def test_a_timed_out_gate_is_never_a_pass(self) -> None:
        self.behaviors["ticket-gate"] = lambda r: SessionResult(text="GATE:PASS", timed_out=True, returncode=1)
        result = self.go(ticket(1))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.forge.merges, [])
        self.assertIn("ralph:failed:issue", self.labels(1))


class UsageConfigTest(unittest.TestCase):
    def test_defaults_and_values(self) -> None:
        config = parse_config({"verify_commands": ["x"]})
        self.assertEqual((config.wait_for_reset, config.usage_wait_seconds), (False, 1800))
        config = parse_config({"verify_commands": ["x"], "wait_for_reset": True, "usage_wait_seconds": 60})
        self.assertEqual((config.wait_for_reset, config.usage_wait_seconds), (True, 60))
        self.assertEqual(replace(config, usage_wait_seconds=1).usage_wait_seconds, 1)

    def test_bad_values_are_refused(self) -> None:
        for extra in ({"wait_for_reset": 1}, {"wait_for_reset": "yes"}, {"usage_wait_seconds": 0}, {"usage_wait_seconds": "5"}):
            with self.subTest(extra), self.assertRaises(ConfigError):
                parse_config({"verify_commands": ["x"], **extra})


if __name__ == "__main__":
    unittest.main()
