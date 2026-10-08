"""Final review fix rounds (#58): FAIL -> one fix session -> scoped re-review."""
import unittest
from pathlib import Path

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.gate_scope import Scope, resolve_gate_scope
from conductor.git_adapter import GitCli
from conductor.ports import Issue, SessionResult
from conductor.run import EXIT_INCOMPLETE, EXIT_OK, run
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
INTEGRATION = "feat/52-final-fix-prd"
PASS = "all good\nGATE:PASS"
FAIL = """FAIL
### Follow-up findings
- FOLLOWUP-SENTINEL rename a helper
### Blocking findings
- BLOCKING-SENTINEL src/a.py:3 off by one
GATE:FAIL"""


def only_in_final_fix(command: str) -> str:
    """A verify command that applies in the final-fix worktree and passes in ticket worktrees."""
    return f'if [ "$(basename "$PWD")" = final-fix ]; then {command}; fi'


class FinalFixBase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Final fix PRD")])
        self.forge.add_sub_issues(
            PRD,
            [Issue(54, "queued ticket", frozenset({"ralph:queued"})), Issue(55, "done", frozenset({"ralph:integrated"}))],
        )
        self.git = GitCli(self.repo.checkout)

    def origin_head(self) -> str:
        line = self.repo.git("ls-remote", "origin", f"refs/heads/{INTEGRATION}")
        return line.split()[0]

    def track_real_head(self) -> None:
        """The PR head is the integration branch on origin, like on GitHub."""
        self.forge.pr_head_sha = lambda number: self.origin_head()

    def go(self, reviews, fix=None, rounds=2, autonomy="respect-hitl-arch", verify="true"):
        config = Config(verify_commands=(verify,), gate_fix_rounds=rounds).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root, autonomy=autonomy
        )
        self.notes = notes_path(config)
        self.notes.parent.mkdir(parents=True, exist_ok=True)
        self.notes.write_text("notes")
        texts = list(reviews)
        self.agents = FakeAgents(
            {
                "implementer": commits_file("feature.txt"),
                "ticket-gate": says("GATE:PASS"),
                "final-review": lambda r: SessionResult(text=texts.pop(0) if len(texts) > 1 else texts[0]),
                "final-fix": fix or commits_file("fixed.txt", "fixed\nRALPH:DONE"),
            }
        )
        return run(config, self.forge, self.agents, self.git)

    def sessions(self, role: str):
        return [r for r in self.agents.requests if r.role == role]

    def final_pr(self) -> dict:
        (pr,) = [p for p in self.forge.prs.values() if p["base"] == "main"]
        return pr

    def prd_labels(self) -> frozenset[str]:
        return self.forge.get_issue(PRD).labels

    def verdict_comments(self) -> list[str]:
        return [b for items in self.forge.comments.values() for _, b in items if b.startswith("## Gate verdict")]

    def all_comments(self) -> str:
        return "\n".join(b for items in self.forge.comments.values() for _, b in items)


class FixRoundTest(FinalFixBase):
    def test_a_fail_starts_one_fix_session_with_only_the_blocking_findings_then_a_scoped_re_review(self) -> None:
        self.track_real_head()
        head_before = self.repo.git("rev-parse", "origin/main")  # the integration branch starts here
        result = self.go([FAIL, PASS])
        self.assertEqual(result.exit_code, EXIT_OK)
        (fix,) = self.sessions("final-fix")
        self.assertIn("BLOCKING-SENTINEL", fix.prompt)
        self.assertNotIn("FOLLOWUP-SENTINEL", fix.prompt)
        first, second = self.sessions("final-review")
        self.assertNotIn("RE-REVIEW", first.prompt)
        head_after = self.origin_head()
        self.assertNotEqual(head_before, head_after)
        self.assertIn("RE-REVIEW", second.prompt)
        self.assertIn(f"PREV = `{head_before}`", second.prompt)
        self.assertIn(f"HEAD = `{head_after}`", second.prompt)
        self.assertIn("RESOLVED or UNRESOLVED", second.prompt)
        previous = Path(second.prompt.split("is in `")[1].split("`")[0])
        self.assertIn("BLOCKING-SENTINEL", previous.read_text())
        self.assertIn(previous.parent, second.add_dirs)
        self.assertEqual(self.final_pr()["state"], "merged")
        self.assertEqual(self.forge.merges[-1][2], head_after)

    def test_followup_findings_stay_in_the_verdict_comment(self) -> None:
        self.track_real_head()
        self.go([FAIL, PASS])
        comments = self.verdict_comments()
        self.assertEqual(len(comments), 2)
        self.assertIn("FOLLOWUP-SENTINEL", comments[0])

    def test_the_fix_session_is_a_writer_with_the_notes_pointer_and_its_own_worktree(self) -> None:
        self.track_real_head()
        self.go([FAIL, PASS])
        (fix,) = self.sessions("final-fix")
        self.assertIn(str(self.notes), fix.prompt)
        self.assertIn(self.notes.parent, fix.add_dirs)
        self.assertIsNone(fix.agent)
        self.assertEqual(fix.cwd.name, "final-fix")
        self.assertEqual(self.repo.git("worktree", "list").count("\n"), 0)

    def test_the_fix_is_pushed_to_the_integration_branch_only_after_verify(self) -> None:
        self.track_real_head()
        self.go([FAIL, PASS], verify=only_in_final_fix("test -f fixed.txt"))
        self.assertIn("fixed.txt", self.repo.git("ls-tree", "-r", "--name-only", self.origin_head()))

    def test_a_fix_that_fails_verify_is_never_pushed_and_blocks_the_prd(self) -> None:
        self.track_real_head()
        head_before = self.repo.git("rev-parse", "origin/main")  # the integration branch starts here
        result = self.go([FAIL, PASS], verify=only_in_final_fix("false"))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.origin_head(), head_before)
        self.assertEqual(len(self.sessions("final-review")), 1)
        self.assertIn("ralph:blocked", self.prd_labels())
        self.assertEqual(self.final_pr()["state"], "open")

    def test_a_blocked_or_unusable_fix_session_blocks_the_prd_without_another_review(self) -> None:
        for name, text in [("blocked", "nope\nRALPH:BLOCKED needs a decision"), ("unusable", "whatever")]:
            with self.subTest(name):
                self.repo.cleanup()
                self.setUp()
                result = self.go([FAIL, PASS], fix=says(text))
                self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
                self.assertEqual(len(self.sessions("final-review")), 1)
                self.assertIn("ralph:blocked", self.prd_labels())
                self.assertEqual(self.final_pr()["state"], "open")

    def test_a_usage_limit_in_the_fix_session_labels_nothing(self) -> None:
        result = self.go([FAIL], fix=lambda r: SessionResult(usage_limit=True))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertNotIn("ralph:blocked", self.prd_labels())

    def test_a_re_review_that_is_unparsable_twice_blocks_the_prd(self) -> None:
        self.track_real_head()
        result = self.go([FAIL, "no marker"])
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(len(self.sessions("final-review")), 3)  # first review + re-review and its retry
        self.assertIn("ralph:blocked", self.prd_labels())
        self.assertEqual(self.final_pr()["state"], "open")


class BoundTest(FinalFixBase):
    def test_after_gate_fix_rounds_failed_rounds_the_pr_stays_open_with_a_diagnosis(self) -> None:
        self.track_real_head()
        count = iter(range(10))
        result = self.go(
            [FAIL], rounds=2, fix=lambda r: commits_file(f"fix-{next(count)}.txt", "fixed\nRALPH:DONE")(r)
        )
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(len(self.sessions("final-fix")), 2)
        self.assertEqual(len(self.sessions("final-review")), 3)
        self.assertEqual(self.final_pr()["state"], "open")
        self.assertIn("ralph:blocked", self.prd_labels())
        self.assertEqual(self.forge.ready, [])
        self.assertEqual(self.forge.merges[-1][1], "merge")  # only the ticket merge, never the final one
        self.assertIn("after 2 fix round(s)", self.all_comments())
        self.assertIn("BLOCKING-SENTINEL", self.all_comments().split("after 2 fix round(s)")[1])

    def test_zero_rounds_means_no_fix_session(self) -> None:
        result = self.go([FAIL], rounds=0)
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.sessions("final-fix"), [])
        self.assertIn("ralph:blocked", self.prd_labels())


class FallbackToFullReviewTest(FinalFixBase):
    def second_review(self) -> str:
        return self.sessions("final-review")[1].prompt

    def test_a_head_that_did_not_move_after_the_fix_gets_a_full_review(self) -> None:
        self.go([FAIL, PASS])  # the fake PR head is constant
        self.assertNotIn("RE-REVIEW", self.second_review())
        self.assertIn("Full two-axis review", self.second_review())

    def test_a_reviewed_commit_that_is_not_an_ancestor_of_the_head_gets_a_full_review(self) -> None:
        heads = iter([self.repo.git("rev-parse", "origin/main")] + ["a" * 40] * 10)
        self.forge.pr_head_sha = lambda number: next(heads)
        self.go([FAIL, PASS])
        self.assertNotIn("RE-REVIEW", self.second_review())

    def test_a_previous_verdict_that_cannot_be_saved_gets_a_full_review(self) -> None:
        self.track_real_head()
        config_state = self.repo.state_root
        final_dir = config_state / f"prd-{PRD}" / "final"
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        final_dir.write_text("a file where the directory should be")
        self.go([FAIL, PASS])
        self.assertNotIn("RE-REVIEW", self.second_review())


class ResolveGateScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.git = GitCli(self.repo.checkout)
        self.base = self.repo.git("rev-parse", "HEAD")
        (self.repo.checkout / "a.txt").write_text("a\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-m", "a")
        self.child = self.repo.git("rev-parse", "HEAD")
        self.repo.git("push", "origin", "HEAD:refs/heads/work")
        self.repo.git("checkout", "-b", "side", self.base)
        (self.repo.checkout / "b.txt").write_text("b\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-m", "b")
        self.sibling = self.repo.git("rev-parse", "HEAD")
        self.repo.git("push", "origin", "HEAD:refs/heads/side")
        self.verdict = self.repo.root / "verdict.md"
        self.verdict.write_text("FAIL\n")

    def scope(self, *, round_no=2, prev=None, head=None, verdict="default") -> Scope:
        return resolve_gate_scope(
            self.git,
            round_no=round_no,
            prev_sha=prev or self.base,
            head_sha=head or self.child,
            prev_verdict=self.verdict if verdict == "default" else verdict,
        )

    def test_a_trusted_fix_gets_the_fix_diff(self) -> None:
        self.assertIs(self.scope(), Scope.FIX_DIFF)

    def test_the_first_round_is_always_full(self) -> None:
        self.assertIs(self.scope(round_no=1), Scope.FULL)

    def test_a_missing_or_empty_previous_verdict_is_full(self) -> None:
        self.assertIs(self.scope(verdict=None), Scope.FULL)
        self.assertIs(self.scope(verdict=self.repo.root / "absent.md"), Scope.FULL)
        self.verdict.write_text("")
        self.assertIs(self.scope(), Scope.FULL)

    def test_an_unchanged_head_is_full(self) -> None:
        self.assertIs(self.scope(prev=self.child, head=self.child), Scope.FULL)

    def test_a_non_ancestor_is_full(self) -> None:
        self.assertIs(self.scope(prev=self.sibling, head=self.child), Scope.FULL)
        self.assertIs(self.scope(prev=self.child, head=self.base), Scope.FULL)

    def test_invalid_shas_are_full_and_never_reach_git(self) -> None:
        self.assertIs(self.scope(prev="--upload-pack=x", head=self.child), Scope.FULL)
        self.assertIs(self.scope(prev=self.base, head="HEAD"), Scope.FULL)
        self.assertIs(self.scope(prev=self.base[:10], head=self.child), Scope.FULL)

    def test_a_head_that_cannot_be_fetched_is_full(self) -> None:
        self.assertIs(self.scope(head="b" * 40), Scope.FULL)


if __name__ == "__main__":
    unittest.main()
