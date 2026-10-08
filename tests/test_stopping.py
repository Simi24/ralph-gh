"""Unit tests for the stop mechanics: state, STOP file, process groups,
signal handlers, requeue and startup reconciliation. No sleeps: processes
are only killed after they said they are ready."""
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

from conductor.config import Config
from conductor.ports import Issue, PullRequest
from conductor.reconcile import reconcile_board, resumable
from conductor.sessions import SessionRegistry, Stopped
from conductor.signals import EXIT_SIGHUP, install_signal_handlers
from conductor.stopping import REASON_DRAIN, REASON_IMMEDIATE, StopState, requeue_in_flight, write_stop_file
from conductor.verify import check_verify
from tests.fakes import FakeForge

PRD = 52
INTEGRATION = "feat/52-integration"
IN_PROGRESS = frozenset({"ralph:in-progress"})
IN_REVIEW = frozenset({"ralph:in-review"})


class StopStateTest(unittest.TestCase):
    def test_nothing_requested_means_no_result(self) -> None:
        stop = StopState()
        self.assertFalse(stop.draining)
        self.assertIsNone(stop.result())

    def test_a_drain_is_a_clean_stop_and_the_first_reason_wins(self) -> None:
        stop = StopState()
        stop.request_drain("usage limit", 3)
        stop.request_drain()
        self.assertTrue(stop.draining)
        self.assertFalse(stop.immediate)
        self.assertEqual((stop.result().exit_code, stop.result().reason), (3, "usage limit"))

    def test_the_first_sigint_drains_and_the_second_stops_immediately(self) -> None:
        stop = StopState()
        stop.escalate()
        self.assertEqual((stop.draining, stop.immediate, stop.result().reason), (True, False, REASON_DRAIN))
        stop.escalate()
        self.assertEqual((stop.immediate, stop.result().reason), (True, REASON_IMMEDIATE))

    def test_the_flag_is_set_before_any_session_is_killed(self) -> None:
        stop = StopState()
        seen: list[bool] = []
        stop.sessions.kill_all = lambda: seen.append(stop.immediate)  # type: ignore[method-assign]
        stop.request_immediate()
        self.assertEqual(seen, [True])

    def test_sessions_are_killed_once(self) -> None:
        stop = StopState()
        calls: list[int] = []
        stop.sessions.kill_all = lambda: calls.append(1)  # type: ignore[method-assign]
        stop.request_immediate()
        stop.request_immediate()
        self.assertEqual(calls, [1])


class StopFileTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "state"

    def test_the_file_drains_once_and_is_consumed(self) -> None:
        stop = StopState(self.root / "STOP")
        stop.poll_file()
        self.assertFalse(stop.draining)
        write_stop_file(self.root)  # what `ralph-gh stop` does
        stop.poll_file()
        self.assertEqual(stop.result().reason, REASON_DRAIN)
        self.assertFalse((self.root / "STOP").exists())

    def test_a_stale_file_is_removed_at_startup(self) -> None:
        write_stop_file(self.root)
        stop = StopState(self.root / "STOP")
        stop.clear_stale_file()
        stop.poll_file()
        self.assertFalse(stop.draining)

    def test_without_a_file_path_nothing_happens(self) -> None:
        stop = StopState()
        stop.poll_file()
        stop.clear_stale_file()
        self.assertFalse(stop.draining)


class SessionRegistryTest(unittest.TestCase):
    def ready_process(self, registry: SessionRegistry, script: str) -> "subprocess.Popen[str]":
        proc = registry.spawn(["sh", "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        self.assertEqual(proc.stdout.readline().strip(), "ready")  # blocks until the script is set up
        return proc

    def test_sigterm_then_sigkill_go_to_the_process_group(self) -> None:
        calls: list[tuple[int, int]] = []
        registry = SessionRegistry(killpg=lambda pgid, sig: calls.append((pgid, sig)), grace=0)
        proc = self.ready_process(registry, "echo ready; exec sleep 600")
        registry.kill_all()
        self.assertEqual(calls, [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)

    def test_a_session_is_in_its_own_group_and_dies_on_sigterm(self) -> None:
        registry = SessionRegistry(grace=30)
        proc = self.ready_process(registry, "echo ready; sleep 600")
        self.assertEqual(os.getpgid(proc.pid), proc.pid)  # its own group
        self.assertNotEqual(os.getpgid(proc.pid), os.getpgid(0))
        registry.kill_all()
        self.assertEqual(proc.wait(), -signal.SIGTERM)

    def test_a_session_that_ignores_sigterm_is_killed_after_the_grace_period(self) -> None:
        registry = SessionRegistry(grace=0.2)
        proc = self.ready_process(registry, "trap '' TERM; echo ready; sleep 600")
        registry.kill_all()
        self.assertEqual(proc.wait(), -signal.SIGKILL)

    def test_a_closed_registry_refuses_new_sessions(self) -> None:
        registry = SessionRegistry()
        registry.kill_all()
        with self.assertRaises(Stopped):
            registry.spawn(["true"])
        with self.assertRaises(Stopped):
            registry.run("true", shell=True)

    def test_released_sessions_are_left_alone(self) -> None:
        calls: list[int] = []
        registry = SessionRegistry(killpg=lambda pgid, sig: calls.append(pgid))
        result = registry.run("exit 3", shell=True)
        self.assertEqual(result.returncode, 3)
        registry.kill_all()
        self.assertEqual(calls, [])

    def test_verify_commands_run_through_the_registry_when_given_one(self) -> None:
        registry = SessionRegistry()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(check_verify(("true",), Path(tmp), registry).ok)
            self.assertFalse(check_verify(("echo boom; exit 1",), Path(tmp), registry).ok)


class SignalHandlersTest(unittest.TestCase):
    def installed(self, stop: StopState):
        restore = install_signal_handlers(stop)
        self.addCleanup(restore)
        return lambda signum: signal.getsignal(signum)(signum, None)

    def test_sigint_twice_and_sigterm(self) -> None:
        stop = StopState()
        deliver = self.installed(stop)
        deliver(signal.SIGINT)
        self.assertEqual((stop.draining, stop.immediate), (True, False))
        deliver(signal.SIGINT)
        self.assertTrue(stop.immediate)
        term = StopState()
        self.installed(term)(signal.SIGTERM)
        self.assertEqual((term.immediate, term.result().reason), (True, REASON_IMMEDIATE))

    def test_sighup_is_an_immediate_stop_with_exit_129(self) -> None:
        stop = StopState()
        self.installed(stop)(signal.SIGHUP)
        self.assertEqual((stop.result().exit_code, stop.result().reason), (EXIT_SIGHUP, "killed (SIGHUP)"))

    def test_the_previous_handlers_are_restored(self) -> None:
        before = signal.getsignal(signal.SIGINT)
        install_signal_handlers(StopState())()
        self.assertIs(signal.getsignal(signal.SIGINT), before)


class RequeueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(verify_commands=("true",)).with_run(prd=PRD, repo_root=Path("."), state_root=Path("."))
        self.forge = FakeForge([Issue(1, "one", IN_PROGRESS), Issue(2, "two", IN_PROGRESS), Issue(3, "three", IN_REVIEW)])
        self.forge.prs[101] = {"head": "feat/52-ticket-2", "base": INTEGRATION, "state": "open"}
        self.forge.prs[102] = {"head": "feat/52-ticket-3", "base": INTEGRATION, "state": "open"}
        self.tickets = [self.forge.issues[n] for n in (1, 2, 3)]

    def requeue(self, stop: StopState) -> None:
        requeue_in_flight(stop, self.config, self.forge, INTEGRATION, self.tickets)

    def labels(self, n: int) -> frozenset[str]:
        return self.forge.issues[n].labels

    def test_nothing_happens_unless_the_stop_is_immediate(self) -> None:
        stop = StopState()
        stop.request_drain()
        self.requeue(stop)
        self.assertEqual(self.forge.label_trail, {})

    def test_a_ticket_without_an_open_pr_is_requeued_and_one_with_a_pr_stays_in_review(self) -> None:
        stop = StopState()
        stop.request_immediate()
        self.requeue(stop)
        self.assertEqual(self.labels(1), frozenset({"ralph:queued"}))
        self.assertEqual(self.labels(2), IN_REVIEW)
        self.assertEqual(self.labels(3), IN_REVIEW)
        self.assertIn("back in the queue", self.forge.comments[1][0][1])
        self.assertIn("PR #101 stays open", self.forge.comments[2][0][1])

    def test_a_closed_or_merged_pr_is_not_an_open_pr(self) -> None:
        self.forge.prs[101]["state"] = "closed"
        stop = StopState()
        stop.request_immediate()
        self.requeue(stop)
        self.assertEqual(self.labels(2), frozenset({"ralph:queued"}))

    def test_an_unreadable_pr_leaves_that_ticket_alone_and_the_others_are_still_done(self) -> None:
        real = self.forge.find_pr

        def flaky(*, head: str, base: str):
            if head.endswith("ticket-1"):
                raise RuntimeError("api down")
            return real(head=head, base=base)

        self.forge.find_pr = flaky  # type: ignore[method-assign]
        stop = StopState()
        stop.request_immediate()
        self.requeue(stop)
        self.assertEqual(self.labels(1), IN_PROGRESS)  # fail closed: a later run reconciles it
        self.assertEqual(self.labels(2), IN_REVIEW)


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(verify_commands=("true",)).with_run(prd=PRD, repo_root=Path("."), state_root=Path("."))
        self.forge = FakeForge([])

    def pr(self, ticket: int, state: str) -> int:
        number = 100 + len(self.forge.prs) + 1
        self.forge.prs[number] = {"head": f"feat/52-ticket-{ticket}", "base": INTEGRATION, "state": state}
        return number

    def reconcile(self, *tickets: Issue) -> list[Issue]:
        self.forge.add_sub_issues(PRD, list(tickets))
        reconcile_board(self.config, self.forge, INTEGRATION, list(tickets))
        return self.forge.list_sub_issues(PRD)

    def test_a_merged_pr_means_integrated(self) -> None:
        self.pr(1, "merged")
        (one,) = self.reconcile(Issue(1, "one", IN_REVIEW))
        self.assertEqual(one.labels, frozenset({"ralph:integrated"}))
        self.assertTrue(self.forge.comments[1])

    def test_a_pr_closed_without_merging_means_queued(self) -> None:
        self.pr(1, "closed")
        (one,) = self.reconcile(Issue(1, "one", IN_REVIEW))
        self.assertEqual(one.labels, frozenset({"ralph:queued"}))

    def test_in_progress_without_a_pr_means_queued(self) -> None:
        (one,) = self.reconcile(Issue(1, "one", IN_PROGRESS))
        self.assertEqual(one.labels, frozenset({"ralph:queued"}))

    def test_an_open_pr_stays_in_review_and_is_resumable(self) -> None:
        number = self.pr(1, "open")
        (one, two) = self.reconcile(Issue(1, "one", IN_PROGRESS), Issue(2, "two", IN_REVIEW))
        self.assertEqual(one.labels, IN_REVIEW)
        found = resumable(self.config, self.forge, INTEGRATION, [one, two])
        self.assertEqual(found, [(one, PullRequest(number, "feat/52-ticket-1", INTEGRATION))])

    def test_the_latest_pr_wins(self) -> None:
        self.pr(1, "closed")
        self.pr(1, "merged")
        (one,) = self.reconcile(Issue(1, "one", IN_REVIEW))
        self.assertEqual(one.labels, frozenset({"ralph:integrated"}))

    def test_other_tickets_are_never_touched(self) -> None:
        tickets = [
            Issue(1, "q", frozenset({"ralph:queued"})),
            Issue(2, "f", frozenset({"ralph:failed:issue"})),
            Issue(3, "i", frozenset({"ralph:integrated"})),
            Issue(4, "closed", IN_REVIEW, state="closed"),
        ]
        self.reconcile(*tickets)
        self.assertEqual(self.forge.label_trail, {})

    def test_an_unreadable_pr_leaves_the_ticket_exactly_as_it_is(self) -> None:
        def boom(*, head: str, base: str):
            raise RuntimeError("api down")

        self.forge.latest_pr = boom  # type: ignore[method-assign]
        (one,) = self.reconcile(Issue(1, "one", IN_REVIEW))
        self.assertEqual(one.labels, IN_REVIEW)


if __name__ == "__main__":
    unittest.main()
