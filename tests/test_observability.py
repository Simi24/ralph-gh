import logging
import unittest
from datetime import datetime, timedelta, timezone

from conductor.config import Config
from conductor.git_adapter import GitCli
from conductor.heartbeat import Heartbeat
from conductor.observer import Observer
from conductor.ports import Issue
from conductor.run import run
from conductor.status_comment import HEADER, MAX_BODY, StatusComment
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
TICKET = 54
ISO = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}"


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.t

    def advance(self, **kwargs: float) -> None:
        self.t += timedelta(**kwargs)


class StatusCommentTest(unittest.TestCase):
    def test_every_post_edits_one_comment_with_timestamped_lines(self) -> None:
        forge = FakeForge([Issue(PRD, "prd")])
        clock = FakeClock()
        status = StatusComment(forge, PRD, clock)
        status.post("#54 dispatched")
        clock.advance(minutes=1)
        status.post("#55 dispatched")
        (comment,) = forge.list_comments(PRD)
        self.assertTrue(comment.body.startswith(HEADER))
        self.assertIn("- 2026-10-08T12:00:00+00:00 #54 dispatched", comment.body)
        self.assertIn("- 2026-10-08T12:01:00+00:00 #55 dispatched", comment.body)

    def test_a_new_run_continues_the_existing_comment(self) -> None:
        forge = FakeForge([Issue(PRD, "prd")])
        StatusComment(forge, PRD, FakeClock()).post("#54 first")
        StatusComment(forge, PRD, FakeClock()).post("#54 second")
        (comment,) = forge.list_comments(PRD)
        self.assertIn("first", comment.body)
        self.assertIn("second", comment.body)

    def test_unreadable_comments_never_create_a_second_one(self) -> None:
        forge = FakeForge([Issue(PRD, "prd")])

        def boom(number: int) -> list:
            raise RuntimeError("api down")

        forge.list_comments = boom  # type: ignore[method-assign]
        StatusComment(forge, PRD, FakeClock()).post("#54 x")
        self.assertEqual(forge.comments, {})

    def test_write_failure_is_fail_open(self) -> None:
        forge = FakeForge([Issue(PRD, "prd")])

        def boom(number: int, body: str) -> int:
            raise RuntimeError("api down")

        forge.create_comment = boom  # type: ignore[method-assign]
        StatusComment(forge, PRD, FakeClock()).post("#54 x")  # must not raise

    def test_long_history_drops_the_oldest_lines_and_keeps_the_header(self) -> None:
        forge = FakeForge([Issue(PRD, "prd")])
        status = StatusComment(forge, PRD, FakeClock())
        for i in range(2000):
            status.post(f"#54 line {i} " + "x" * 50)
        (comment,) = forge.list_comments(PRD)
        self.assertLessEqual(len(comment.body), MAX_BODY)
        self.assertTrue(comment.body.startswith(HEADER))
        self.assertIn("line 1999", comment.body)
        self.assertNotIn("line 0 ", comment.body)


class HeartbeatTest(unittest.TestCase):
    def test_at_most_one_beat_per_five_minutes_and_only_while_a_session_runs(self) -> None:
        clock = FakeClock()
        posts: list[str] = []
        beat = Heartbeat(posts.append, clock)
        clock.advance(minutes=10)
        self.assertFalse(beat.tick())  # nothing running
        beat.session_started("implementer #54")
        clock.advance(minutes=4)
        self.assertFalse(beat.tick())
        clock.advance(minutes=1)
        self.assertTrue(beat.tick())
        clock.advance(minutes=1)
        self.assertFalse(beat.tick())
        clock.advance(minutes=4)
        self.assertTrue(beat.tick())
        beat.session_ended("implementer #54")
        clock.advance(minutes=10)
        self.assertFalse(beat.tick())
        self.assertEqual(len(posts), 2)
        self.assertIn("implementer #54", posts[0])


class ObservedRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Observed PRD")])
        self.forge.add_sub_issues(PRD, [Issue(TICKET, "t", frozenset({"ralph:queued"}))])
        self.clock = FakeClock()
        self.config = Config(verify_commands=("true",)).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root
        )

    def observed_run(self, agents: FakeAgents):
        observer = Observer(
            self.forge, prd=PRD, state_root=self.repo.state_root, repo_root=self.repo.checkout, clock=self.clock
        )
        result = run(self.config, self.forge, agents, GitCli(self.repo.checkout), observer)
        return result

    def agents(self, **overrides) -> FakeAgents:
        behaviors = {"implementer": commits_file("f.txt"), "ticket-gate": says("GATE:PASS")}
        behaviors.update(overrides)
        return FakeAgents(behaviors)

    def test_one_status_comment_with_a_stamped_line_per_transition_naming_the_ticket(self) -> None:
        self.observed_run(self.agents())
        (comment,) = self.forge.list_comments(PRD)
        lines = comment.body.splitlines()[2:]
        for line in lines:
            self.assertRegex(line, rf"^- {ISO} (#\d+|PRD) ")
        text = comment.body
        for expected in (
            "#54 dispatched",
            "#54 implementer session started",
            "#54 verify started",
            "#54 PR #101 opened",
            "#54 in review",
            "#54 ticket-gate session started",
            "PR #101 merged",
            "#54 integrated",
            "run ended: integrated",
        ):
            self.assertIn(expected, text)

    def test_failure_and_blocked_are_status_lines(self) -> None:
        self.observed_run(self.agents(implementer=says("stuck\nRALPH:BLOCKED nope")))
        (comment,) = self.forge.list_comments(PRD)
        self.assertIn("#54 blocked", comment.body)

    def test_last_run_has_per_ticket_phase_timings_and_ends_with_the_exit_reason(self) -> None:
        self.observed_run(self.agents())
        text = (self.repo.state_root / "last-run.md").read_text()
        self.assertIn("## Ticket #54 timing", text)
        for phase in ("implementer", "verify", "gate"):
            self.assertRegex(text, rf"- {phase}: \d+s")
        self.assertIn("- fix: none", text)
        self.assertEqual(text.rstrip("\n").splitlines()[-1], "Exit reason: integrated")

    def test_last_run_ends_with_the_exit_reason_after_a_crash(self) -> None:
        def explode(request):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.observed_run(self.agents(implementer=explode))
        text = (self.repo.state_root / "last-run.md").read_text()
        self.assertEqual(text.rstrip("\n").splitlines()[-1], "Exit reason: error (RuntimeError)")

    def test_last_run_is_never_without_an_exit_reason_even_mid_run(self) -> None:
        seen: list[str] = []

        def peek(request):
            seen.append((self.repo.state_root / "last-run.md").read_text())
            return commits_file("f.txt")(request)

        self.observed_run(self.agents(implementer=peek))
        self.assertTrue(seen[0].rstrip("\n").splitlines()[-1].startswith("Exit reason: interrupted"))

    def test_every_run_log_line_is_timestamped(self) -> None:
        self.observed_run(self.agents())
        lines = (self.repo.state_root / "run.log").read_text().splitlines()
        self.assertGreater(len(lines), 5)
        for line in lines:
            self.assertRegex(line, rf"^{ISO} ")

    def test_run_log_handler_is_detached_after_the_run(self) -> None:
        self.observed_run(self.agents())
        self.assertEqual(logging.getLogger("conductor").handlers, [])

    def test_without_an_observer_nothing_is_written(self) -> None:
        run(self.config, self.forge, self.agents(), GitCli(self.repo.checkout))
        self.assertFalse((self.repo.state_root / "last-run.md").exists())
        self.assertEqual(self.forge.comments, {})


if __name__ == "__main__":
    unittest.main()
