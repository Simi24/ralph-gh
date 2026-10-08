import unittest

from conductor.config import Config
from conductor.git_adapter import GitCli
from conductor.ports import Issue, SessionResult
from conductor.run import EXIT_INCOMPLETE, EXIT_OK, run
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
QUEUED = 54
OTHER = 55
INTEGRATION = "feat/52-final-pr-prd"
PASS = "reviewed\nGATE:PASS"


class FinalPrTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Final PR PRD")])
        self.set_tickets(frozenset({"ralph:integrated"}))
        self.git = GitCli(self.repo.checkout)

    def set_tickets(self, other_labels: frozenset[str]) -> None:
        self.forge.add_sub_issues(
            PRD,
            [Issue(QUEUED, "queued ticket", frozenset({"ralph:queued"})), Issue(OTHER, "other", other_labels)],
        )

    def go(self, review=None, autonomy="respect-hitl-arch", allowlist=(), gate=None):
        config = Config(verify_commands=("test -f feature.txt",), yolo_allowlist=allowlist).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root, autonomy=autonomy
        )
        self.agents = FakeAgents(
            {
                "implementer": commits_file("feature.txt"),
                "ticket-gate": gate or says("GATE:PASS"),
                "final-review": review or says(PASS),
            }
        )
        return run(config, self.forge, self.agents, self.git)

    def final_pr(self) -> tuple[int, dict]:
        (item,) = [(n, p) for n, p in self.forge.prs.items() if p["base"] == "main"]
        return item

    def reviews(self) -> int:
        return len([r for r in self.agents.requests if r.role == "final-review"])

    def labels(self, number: int) -> frozenset[str]:
        return self.forge.get_issue(number).labels

    def test_the_draft_is_opened_after_the_first_ticket_merge_and_closes_every_issue(self) -> None:
        seen: list[bool] = []

        def gate(request):
            seen.append(any(p["base"] == "main" for p in self.forge.prs.values()))
            return says("GATE:PASS")(request)

        self.go(gate=gate, autonomy="halt-each-pr")
        self.assertEqual(seen, [False])
        _, pr = self.final_pr()
        self.assertEqual(pr["head"], INTEGRATION)
        for n in (PRD, QUEUED, OTHER):
            self.assertIn(f"Closes #{n}", pr["body"])

    def test_the_final_pr_title_is_a_conventional_commit(self) -> None:
        self.go()
        self.assertEqual(self.final_pr()[1]["title"], "feat: Final PR PRD")

    def test_the_review_starts_only_when_every_ticket_is_integrated(self) -> None:
        self.set_tickets(frozenset({"ralph:blocked"}))
        result = self.go()
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.reviews(), 0)
        self.assertEqual(self.forge.ready, [])
        self.assertEqual([m[1] for m in self.forge.merges], ["merge"])  # only the ticket merge
        self.assertTrue(self.final_pr()[1]["draft"])

    def test_pass_marks_ready_posts_the_verdict_and_merges_squash_pinned_to_the_reviewed_head(self) -> None:
        result = self.go()
        number, pr = self.final_pr()
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(pr["state"], "merged")
        self.assertEqual(self.forge.ready, [number])
        verdicts = [b for n, b in self.forge.comments if n == number and b.startswith("## Gate verdict")]
        self.assertEqual(len(verdicts), 1)
        self.assertIn("GATE:PASS", verdicts[0])
        self.assertEqual(self.forge.merges[-1], (number, "squash", self.forge.pr_heads[number]))

    def test_the_reviewer_is_the_configured_agent_without_a_model(self) -> None:
        self.go()
        review = [r for r in self.agents.requests if r.role == "final-review"][0]
        self.assertEqual((review.agent, review.model), ("ralph-gate-reviewer", None))
        self.assertIn(f"#{PRD}", review.prompt)

    def test_tickets_stay_open_and_integrated_until_the_final_pr_merges(self) -> None:
        self.go(autonomy="halt-each-pr")
        for n in (QUEUED, OTHER):
            self.assertIn("ralph:integrated", self.labels(n))
            self.assertEqual(self.forge.get_issue(n).state, "open")
        self.assertEqual(self.forge.closed, [])

    def test_a_merged_final_pr_closes_the_prd_and_every_ticket_as_done(self) -> None:
        self.go()
        self.assertCountEqual(self.forge.closed, [PRD, QUEUED, OTHER])
        for n in (PRD, QUEUED, OTHER):
            self.assertIn("ralph:done", self.labels(n))
        self.assertNotIn("ralph:integrated", self.labels(QUEUED))

    def test_halt_each_pr_never_merges_the_final_pr(self) -> None:
        result = self.go(autonomy="halt-each-pr")
        number, pr = self.final_pr()
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(pr["state"], "open")
        self.assertEqual(self.forge.ready, [number])
        self.assertIn("ralph:gate-passed", self.labels(PRD))

    def test_respect_hitl_arch_withholds_for_a_flagged_prd_or_ticket(self) -> None:
        for name, number, labels in [
            ("ticket", OTHER, frozenset({"ralph:integrated", "ralph:hitl-arch"})),
            ("prd", PRD, frozenset({"ralph:hitl-arch"})),
        ]:
            with self.subTest(name):
                self.tearDown_repo()
                self.setUp()
                self.forge.issues[number] = Issue(number, "x", labels)
                result = self.go()
                self.assertEqual(result.exit_code, EXIT_OK)
                self.assertEqual(self.final_pr()[1]["state"], "open")
                self.assertIn("ralph:gate-passed", self.labels(PRD))

    def tearDown_repo(self) -> None:
        self.repo.cleanup()

    def test_yolo_merges_only_when_every_file_matches_the_allowlist(self) -> None:
        for name, diff, allowlist, merged in [
            ("all match", ["src/a.py", "README.md"], (r"^src/", r"^README\.md$"), True),
            ("one outside", ["src/a.py", "install.sh"], (r"^src/",), False),
            ("unreadable diff", None, (r"^src/",), False),
            ("empty allowlist", ["src/a.py"], (), False),
        ]:
            with self.subTest(name):
                self.tearDown_repo()
                self.setUp()
                self.forge.final_diff = diff
                self.go(autonomy="yolo", allowlist=allowlist)
                self.assertEqual(self.final_pr()[1]["state"] == "merged", merged)

    def test_a_head_that_moved_after_the_review_is_never_merged(self) -> None:
        def review(request):
            number, _ = self.final_pr()
            self.forge.pr_heads[number] = "f" * 40
            return says(PASS)(request)

        result = self.go(review=review)
        _, pr = self.final_pr()
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(result.reason, "head moved after review")
        self.assertEqual(pr["state"], "open")
        self.assertEqual(self.forge.ready, [])

    def test_a_merge_refused_by_the_pin_leaves_the_pr_open(self) -> None:
        original = self.forge.merge_pr

        def refuse_final(number, *, method, head_sha):
            return False if method == "squash" else original(number, method=method, head_sha=head_sha)

        self.forge.merge_pr = refuse_final
        result = self.go()
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.final_pr()[1]["state"], "open")
        self.assertEqual(self.forge.closed, [])

    def test_a_failing_review_never_merges_and_flags_the_prd_for_a_human(self) -> None:
        result = self.go(review=says("### Blocking findings\n- x\nGATE:FAIL"))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.final_pr()[1]["state"], "open")
        self.assertIn("ralph:blocked", self.labels(PRD))
        self.assertEqual(self.forge.ready, [])
        self.assertTrue(any(b.startswith("## Gate verdict") for _, b in self.forge.comments))

    def test_an_unparsable_review_is_retried_once_and_never_a_pass(self) -> None:
        for name, text in [
            ("prose", "looks fine to me"),
            ("quoted", 'reply "GATE:PASS" when done'),
            ("both", "GATE:PASS\nGATE:FAIL"),
        ]:
            with self.subTest(name):
                self.tearDown_repo()
                self.setUp()
                result = self.go(review=says(text))
                self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
                self.assertEqual(self.reviews(), 2)
                self.assertEqual(self.final_pr()[1]["state"], "open")
                self.assertIn("ralph:blocked", self.labels(PRD))
                self.assertFalse(any(b.startswith("## Gate verdict") for _, b in self.forge.comments))

    def test_an_unparsable_review_followed_by_a_pass_merges(self) -> None:
        answers = iter(["no marker", PASS])
        result = self.go(review=lambda r: says(next(answers))(r))
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.final_pr()[1]["state"], "merged")

    def test_a_timed_out_review_is_never_a_pass(self) -> None:
        result = self.go(review=lambda r: SessionResult(text=PASS, timed_out=True))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.final_pr()[1]["state"], "open")

    def test_a_usage_limit_in_the_review_labels_nothing(self) -> None:
        result = self.go(review=lambda r: SessionResult(text="", usage_limit=True))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.reviews(), 1)
        self.assertNotIn("ralph:blocked", self.labels(PRD))

    def test_the_review_worktree_is_removed(self) -> None:
        self.go()
        self.assertEqual(self.repo.git("worktree", "list").count("\n"), 0)


if __name__ == "__main__":
    unittest.main()
