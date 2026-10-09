"""The integration branch is verified after every ticket merge (L2).

Real temp git repo; `FakeForge.on_merge` makes every PR merge a real merge commit on origin.
The verify command passes or fails on the MERGED content: `usage.txt` (ticket 2) may only exist
if `defs.txt` still says `old`, which ticket 1 removes. Each ticket passes verify alone; the
two together do not. Sessions synchronize on events and barriers, never on sleeps.
"""
import subprocess
import threading
import unittest
from unittest import mock

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.git_adapter import GitCli, GitError
from conductor.naming import integration_branch, ticket_branch
from conductor.ports import Blocker, Issue, SessionRequest, SessionResult
from conductor.run import EXIT_INCOMPLETE, EXIT_OK, run
from conductor.stopping import StopState
from tests.fakes import FakeAgents, FakeForge, says
from tests.gitrepo import TempRepo

PRD = 52
INTEGRATION = integration_branch("feat", PRD, "Integration verify PRD")
Q = frozenset({"ralph:queued"})
WAIT = 20  # seconds: a safety net for a broken scenario, never a pacing delay
DONE = "fixed\nRALPH:DONE"


def write_and_commit(request: SessionRequest, name: str, text: str, outcome: str) -> SessionResult:
    (request.cwd / name).write_text(text)
    for args in (["add", "-A"], ["commit", "-m", f"feat: write {name}"]):
        subprocess.run(["git", *args], cwd=request.cwd, check=True, capture_output=True)
    return SessionResult(text=outcome)


def number_of(request: SessionRequest) -> int:
    return int(request.cwd.name.removeprefix("ticket-"))


def ticket(number: int) -> Issue:
    return Issue(number, f"ticket {number}", Q)


class IntegrationVerifyBase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        (self.repo.checkout / "defs.txt").write_text("old\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-m", "defs")
        self.repo.git("push", "origin", "HEAD:refs/heads/main")
        self.log = self.repo.root / "events.log"
        self.log.write_text("")
        self.forge = FakeForge([Issue(PRD, "Integration verify PRD")])
        self.forge.on_merge = self.merge
        self.stop: StopState | None = None
        self.behaviors = {
            "implementer": self.implement,
            "ticket-gate": says("GATE:PASS"),
            "final-review": says("GATE:PASS"),
            "fix": says("RALPH:DONE"),
            "integration-fix": self.fixes_defs,
        }
        # Per-ticket content: 1 removes `old` from defs, 2 starts using it. Alone each is green.
        self.content = {1: ("defs.txt", "new\n"), 2: ("usage.txt", "calls old\n"), 3: ("three.txt", "3\n"), 4: ("four.txt", "4\n")}

    # --- scenario plumbing ---

    def note(self, line: str) -> None:
        with open(self.log, "a") as handle:
            handle.write(line + "\n")

    def lines(self) -> list[str]:
        return self.log.read_text().splitlines()

    def index(self, prefix: str) -> int:
        found = [i for i, line in enumerate(self.lines()) if line.startswith(prefix)]
        self.assertTrue(found, f"{prefix!r} not in the log: {self.lines()}")
        return found[0]

    def merge(self, head: str, base: str) -> None:
        self.repo.merge_branch(head, base)
        self.note(f"merge {head}")

    def implement(self, request: SessionRequest) -> SessionResult:
        n = number_of(request)
        self.note(f"implementer {n}")
        name, text = self.content[n]
        return write_and_commit(request, name, text, "RALPH:DONE")

    def fixes_defs(self, request: SessionRequest) -> SessionResult:
        """Make both tickets' work coexist again: `defs.txt` mentions `old` and `new`."""
        self.note("integration-fix session")
        return write_and_commit(request, "defs.txt", "old new\n", DONE)

    def tip(self) -> str:
        return self.repo.git("rev-parse", f"refs/heads/{INTEGRATION}", cwd=self.repo.origin)

    def verify_command(self) -> str:
        record = f'echo "verify $(basename "$PWD") $(git rev-parse HEAD)" >> {self.log}'
        return f"{record}; if [ -f usage.txt ]; then grep -q old defs.txt; fi"

    def go(self, tickets: list[Issue], parallel: int = 2, rounds: int = 2, **config):
        self.forge.add_sub_issues(PRD, tickets)
        self.agents = FakeAgents(self.behaviors)
        cfg = Config(verify_commands=(self.verify_command(),), parallel=parallel, gate_fix_rounds=rounds, **config).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root, autonomy="halt-each-pr"
        )
        notes = notes_path(cfg)
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("notes")
        self.notes = notes
        return run(cfg, self.forge, self.agents, GitCli(self.repo.checkout), stop=self.stop)

    def roles(self) -> list[str]:
        return [r.role for r in self.agents.requests]

    def sessions(self, role: str) -> list[SessionRequest]:
        return [r for r in self.agents.requests if r.role == role]

    def labels(self, number: int) -> frozenset[str]:
        return self.forge.get_issue(number).labels

    def prd_comments(self) -> str:
        return "\n".join(body for _, body in self.forge.comments.get(PRD, []))

    def assert_no_ticket_failed(self, *numbers: int) -> None:
        for n in numbers:
            self.assertIn("ralph:integrated", self.labels(n))
            self.assertFalse({"ralph:failed:issue", "ralph:failed:systemic", "ralph:blocked"} & self.labels(n))

    def assert_prd_blocked(self, result) -> None:
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertIn("ralph:blocked", self.labels(PRD))
        self.assertIn("integration branch broken after the merge of #", self.prd_comments())
        self.assertIn("integration branch broken after the merge of #", result.reason)
        self.assertNotIn("final-review", self.roles())  # the final review never starts on a red tip


class GreenIntegrationTest(IntegrationVerifyBase):
    def test_the_merge_commit_is_verified_pinned_in_a_throwaway_worktree(self) -> None:
        result = self.go([ticket(3)])
        self.assertEqual(result.exit_code, EXIT_OK)
        verified = [line.split() for line in self.lines() if line.startswith("verify integration-verify")]
        self.assertEqual(verified, [["verify", "integration-verify", self.tip()]])  # exactly the merge commit
        parents = self.repo.git("rev-list", "--parents", "-n1", self.tip(), cwd=self.repo.origin).split()
        self.assertEqual(len(parents), 3)  # a merge commit, not a ticket commit
        self.assertFalse((self.repo.state_root / f"prd-{PRD}" / "worktrees" / "integration-verify").exists())
        self.assertNotIn("integration-fix", self.roles())

    def test_a_dependent_is_dispatched_only_after_the_merge_verified_green_and_merges_the_green_sha(self) -> None:
        self.forge.blockers = {4: [Blocker("", 3, "open")]}
        seen = {}
        base_implement = self.implement

        def implement(request):
            if number_of(request) == 4:
                seen["log"] = self.lines()
                seen["prompt"] = request.prompt
            return base_implement(request)

        self.behaviors["implementer"] = implement
        result = self.go([ticket(3), ticket(4)])
        self.assertEqual(result.exit_code, EXIT_OK)
        merge_of_3 = self.repo.git("rev-list", "--merges", "-n2", f"refs/heads/{INTEGRATION}", cwd=self.repo.origin).split()[-1]
        self.assertIn(f"verify integration-verify {merge_of_3}", seen["log"])  # verified before 4 started
        self.assertIn(f"merge `{merge_of_3}`", seen["prompt"])
        self.assertNotIn(f"merge `origin/{INTEGRATION}`", seen["prompt"])


class RedIntegrationTest(IntegrationVerifyBase):
    def test_a_red_merge_is_repaired_by_an_integration_fix_and_the_run_goes_on_to_the_final_review(self) -> None:
        result = self.go([ticket(1), ticket(2)])
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.roles().count("integration-fix"), 1)
        self.assert_no_ticket_failed(1, 2)
        self.assertNotIn("ralph:blocked", self.labels(PRD))
        self.assertLess(self.roles().index("integration-fix"), self.roles().index("final-review"))
        # re-verified in its own worktree, then pushed on top of the red merge without force
        fix_verified = [line for line in self.lines() if line.startswith("verify integration-fix")]
        self.assertEqual(len(fix_verified), 1)
        self.assertEqual(self.repo.git("show", f"{self.tip()}:defs.txt", cwd=self.repo.origin), "old new")
        self.assertEqual(fix_verified[0].split()[2], self.tip())  # the pushed commit is the verified one
        self.assertEqual(self.forge.write_threads, {threading.get_ident()})  # forge writes stay on the conductor thread

    def test_the_integration_fix_session_gets_pointers_only(self) -> None:
        self.go([ticket(1), ticket(2)])
        (request,) = self.sessions("integration-fix")
        self.assertEqual(request.cwd.name, "integration-fix")
        self.assertIn(f"PRD #{PRD}", request.prompt)
        self.assertIn(str(self.notes), request.prompt)  # notes pointer
        self.assertIn("grep -q old defs.txt", request.prompt)  # the verify output tail and the verify commands
        self.assertIn("git log --merges --first-parent", request.prompt)  # the merge commits since the last green sha
        self.assertRegex(request.prompt, r"merge of ticket\(s\) #[12]\b")  # the ticket whose merge broke it
        self.assertNotIn("ticket 1", request.prompt)  # never ticket text

    def test_nothing_is_dispatched_or_merged_while_the_branch_is_red(self) -> None:
        # Ticket 3 needs both others, so it is ready exactly when the branch is red: it must start only
        # after the fix, and merge the fixed commit.
        self.forge.blockers = {3: [Blocker("", 1, "open"), Blocker("", 2, "open")]}
        result = self.go([ticket(1), ticket(2), ticket(3)])
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertGreater(self.index("implementer 3"), self.index("verify integration-fix"))
        (three,) = [r for r in self.sessions("implementer") if number_of(r) == 3]
        fixed = self.repo.git("rev-list", "--no-merges", "-n1", f"refs/heads/{INTEGRATION}~1", cwd=self.repo.origin)
        self.assertIn("run `git fetch origin`, then merge `", three.prompt)
        self.assertEqual(self.lines()[self.index("verify integration-fix")].split()[2], fixed)
        self.assertIn(f"merge `{fixed}`", three.prompt)  # the last green sha: the pushed fix
        self.assert_no_ticket_failed(1, 2, 3)

    def test_a_ticket_that_reaches_the_merge_step_while_red_waits_for_the_fix(self) -> None:
        red = threading.Event()
        gated = threading.Event()
        implement = self.implement

        def implement_three_late(request):
            if number_of(request) == 3 and not red.wait(WAIT):  # ticket 3 starts only once the branch is red
                raise AssertionError("the branch never went red")
            return implement(request)

        def gate(request):
            if number_of(request) == 3:
                gated.set()
            return SessionResult(text="GATE:PASS")

        def fix(request):
            red.set()
            if not gated.wait(WAIT):  # ticket 3 got to its gate (and then to the merge step) during the fix
                raise AssertionError("ticket 3 never reached its gate")
            return self.fixes_defs(request)

        self.behaviors.update({"implementer": implement_three_late, "ticket-gate": gate, "integration-fix": fix})
        result = self.go([ticket(1), ticket(2), ticket(3)], parallel=3)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertGreater(self.index("merge feat/52-ticket-3"), self.index("verify integration-fix"))
        self.assert_no_ticket_failed(1, 2, 3)

    def test_a_running_session_carries_on_while_the_branch_is_red(self) -> None:
        red = threading.Event()
        three_running = threading.Event()
        implement = self.implement

        def implement_three(request):
            if number_of(request) == 3:
                three_running.set()  # ticket 3 is in its session when the branch goes red ...
                if not red.wait(WAIT):  # ... and is not interrupted by it
                    raise AssertionError("the branch never went red")
            return implement(request)

        def fix(request):
            red.set()
            return self.fixes_defs(request)

        def slow_start(request):
            # tickets 1 and 2 begin only once ticket 3 is running: it holds a slot from the start
            if number_of(request) != 3 and not three_running.wait(WAIT):
                raise AssertionError("ticket 3 never started")
            return implement_three(request)

        self.behaviors.update({"implementer": slow_start, "integration-fix": fix})
        result = self.go([ticket(3), ticket(1), ticket(2)], parallel=3)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.roles().count("implementer"), 3)  # nothing was cancelled
        self.assert_no_ticket_failed(1, 2, 3)


class BrokenIntegrationTest(IntegrationVerifyBase):
    def test_exhausted_fix_rounds_block_the_prd_and_fail_no_ticket(self) -> None:
        rounds = iter(range(9))  # a commit each round, but defs.txt stays wrong
        self.behaviors["integration-fix"] = lambda request: write_and_commit(request, f"noise{next(rounds)}.txt", "x\n", DONE)
        result = self.go([ticket(1), ticket(2)], rounds=2)
        self.assert_prd_blocked(result)
        self.assertEqual(self.roles().count("integration-fix"), 2)  # bounded by gate_fix_rounds
        self.assert_no_ticket_failed(1, 2)
        self.assertIn("grep -q old defs.txt", self.prd_comments())  # the verify tail
        merges = self.repo.git("rev-list", "--merges", f"refs/heads/{INTEGRATION}", cwd=self.repo.origin).split()
        self.assertEqual(self.tip(), merges[0])  # nothing broken was pushed

    def test_a_blocked_integration_fix_blocks_the_prd(self) -> None:
        self.behaviors["integration-fix"] = says("RALPH:BLOCKED cannot tell which ticket is wrong")
        result = self.go([ticket(1), ticket(2)])
        self.assert_prd_blocked(result)
        self.assertEqual(self.roles().count("integration-fix"), 1)
        self.assertIn("cannot tell which ticket is wrong", self.prd_comments())
        self.assert_no_ticket_failed(1, 2)

    def test_an_unparsable_integration_fix_blocks_the_prd(self) -> None:
        self.behaviors["integration-fix"] = says("I think it is fine now")
        result = self.go([ticket(1), ticket(2)])
        self.assert_prd_blocked(result)
        self.assert_no_ticket_failed(1, 2)

    def test_a_fix_that_cannot_be_pushed_blocks_the_prd(self) -> None:
        def fix_then_the_branch_moves(request):
            outcome = self.fixes_defs(request)
            tip = self.tip()
            tree = self.repo.git("rev-parse", f"{tip}^{{tree}}", cwd=self.repo.origin)
            identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
            other = self.repo.git(*identity, "commit-tree", tree, "-p", tip, "-m", "someone else", cwd=self.repo.origin)
            self.repo.git("update-ref", f"refs/heads/{INTEGRATION}", other, tip, cwd=self.repo.origin)
            return outcome

        self.behaviors["integration-fix"] = fix_then_the_branch_moves
        result = self.go([ticket(1), ticket(2)])
        self.assert_prd_blocked(result)
        self.assertIn("Could not push", self.prd_comments())
        self.assert_no_ticket_failed(1, 2)

    def test_a_blocked_run_dispatches_nothing_more_and_a_ticket_at_its_merge_is_parked_not_failed(self) -> None:
        self.forge.blockers = {4: [Blocker("", 1, "open"), Blocker("", 2, "open")]}
        red = threading.Event()
        implement = self.implement

        def implement_three_late(request):
            if number_of(request) == 3 and not red.wait(WAIT):  # ticket 3 gets to its merge step only after the break
                raise AssertionError("the branch never went red")
            return implement(request)

        def blocked_fix(request):
            red.set()
            return SessionResult(text="RALPH:BLOCKED no idea")

        self.behaviors.update({"implementer": implement_three_late, "integration-fix": blocked_fix})
        result = self.go([ticket(1), ticket(2), ticket(3), ticket(4)], parallel=3)
        self.assert_prd_blocked(result)
        self.assertEqual(len(self.forge.merges), 2)  # only the two that made the branch red: ticket 3 never merged
        self.assertEqual(self.labels(3) & {"ralph:in-review", "ralph:integrated", "ralph:failed:issue"}, {"ralph:in-review"})
        self.assertIn("is broken", "\n".join(body for _, body in self.forge.comments.get(3, [])))
        self.assertIn("ralph:queued", self.labels(4))  # never dispatched

class ResumedRunTest(IntegrationVerifyBase):
    def broken_integration_branch(self) -> str:
        """An earlier run left the integration branch red: usage.txt, but defs.txt lost `old`."""
        git = self.repo.git
        git("checkout", "-B", "scratch", "main")
        (self.repo.checkout / "defs.txt").write_text("new\n")
        (self.repo.checkout / "usage.txt").write_text("calls old\n")
        git("add", "-A")
        git("commit", "-m", "feat: both tickets")
        git("push", "origin", f"scratch:refs/heads/{INTEGRATION}")
        git("checkout", "main")
        return self.tip()

    def done_tickets(self) -> list[Issue]:
        return [Issue(1, "ticket 1", frozenset({"ralph:integrated"})), Issue(2, "ticket 2", frozenset({"ralph:integrated"}))]

    def test_a_red_tip_is_repaired_before_the_final_review_starts(self) -> None:
        self.broken_integration_branch()
        result = self.go(self.done_tickets())
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertLess(self.roles().index("integration-fix"), self.roles().index("final-review"))
        self.assertEqual(self.repo.git("show", f"{self.tip()}:defs.txt", cwd=self.repo.origin), "old new")

    def test_an_unrepairable_red_tip_never_reaches_the_final_review(self) -> None:
        self.broken_integration_branch()
        self.behaviors["integration-fix"] = says("RALPH:BLOCKED no")
        result = self.go(self.done_tickets())
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertIn("ralph:blocked", self.labels(PRD))
        self.assertNotIn("final-review", self.roles())

    def test_a_green_tip_ahead_of_the_base_is_verified_then_reviewed(self) -> None:
        self.repo.git("checkout", "-B", "scratch", "main")
        (self.repo.checkout / "three.txt").write_text("3\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-m", "feat: three")
        self.repo.git("push", "origin", f"scratch:refs/heads/{INTEGRATION}")
        self.repo.git("checkout", "main")
        result = self.go([Issue(3, "ticket 3", frozenset({"ralph:integrated"}))])
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual([line.split()[1] for line in self.lines() if line.startswith("verify")], ["integration-verify"])
        self.assertNotIn("integration-fix", self.roles())


class StopAndUsageLimitTest(IntegrationVerifyBase):
    def worktrees_left(self) -> list[str]:
        trees = self.repo.state_root / f"prd-{PRD}" / "worktrees"
        return sorted(p.name for p in trees.glob("integration-*")) if trees.exists() else []

    def test_a_usage_limit_during_an_integration_fix_is_a_pause_never_a_failure(self) -> None:
        self.behaviors["integration-fix"] = lambda request: SessionResult(usage_limit=True, usage_reset="in 2h")
        result = self.go([ticket(1), ticket(2)])
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertTrue(result.reason.startswith("usage limit"), result.reason)
        self.assertNotIn("ralph:blocked", self.labels(PRD))
        self.assert_no_ticket_failed(1, 2)
        self.assertEqual(self.worktrees_left(), [])  # the check's worktrees went with it

    def test_an_immediate_stop_during_an_integration_fix_is_never_a_failure(self) -> None:
        self.stop = StopState(self.repo.root / "STOP")

        def fix_and_stop(request):
            self.stop.request_immediate()
            return SessionResult(text=DONE)

        self.behaviors["integration-fix"] = fix_and_stop
        result = self.go([ticket(1), ticket(2)])
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertIn("stopped", result.reason)
        self.assertNotIn("ralph:blocked", self.labels(PRD))
        self.assert_no_ticket_failed(1, 2)
        self.assertEqual(self.worktrees_left(), [])

    def test_a_graceful_stop_still_lets_the_pending_check_finish(self) -> None:
        self.stop = StopState(self.repo.root / "STOP")

        def stop_on_the_first_gate(request):
            self.stop.request_drain()
            return SessionResult(text="GATE:PASS")

        self.behaviors["ticket-gate"] = stop_on_the_first_gate
        self.go([ticket(3)])
        self.assertTrue([line for line in self.lines() if line.startswith("verify integration-verify")])
        self.assertNotIn("final-review", self.roles())


class StaleWorktreesTest(IntegrationVerifyBase):
    def leave_stale_worktrees(self) -> None:
        """What a SIGKILLed run leaves: every kind of worktree, with their local branches."""
        git = GitCli(self.repo.checkout)
        trees = self.repo.state_root / f"prd-{PRD}" / "worktrees"
        git.add_detached_worktree(trees / "integration-verify", self.repo.git("rev-parse", "HEAD"))
        git.add_worktree(trees / "integration-fix", f"{INTEGRATION}-integration-fix", "main")
        for n in (1, 2):
            git.add_worktree(trees / f"ticket-{n}", ticket_branch("feat", PRD, n), "main")
        (trees / "ticket-1" / "half-written.txt").write_text("crash\n")

    def test_the_next_run_clears_stale_worktrees_and_integrates_with_a_green_check(self) -> None:
        self.leave_stale_worktrees()
        result = self.go([ticket(1), ticket(2)])
        self.assertEqual(result.exit_code, EXIT_OK)  # not ralph:blocked, not failed:systemic
        self.assert_no_ticket_failed(1, 2)
        self.assertNotIn("ralph:blocked", self.labels(PRD))
        self.assertEqual(self.roles().count("integration-fix"), 1)  # the red merge was repaired in a fresh worktree
        self.assertEqual(self.repo.git("show", f"{self.tip()}:defs.txt", cwd=self.repo.origin), "old new")
        trees = self.repo.git("worktree", "list")
        self.assertNotIn("ticket-", trees)
        self.assertNotIn("integration-", trees)

    def test_a_failing_cleanup_is_logged_and_the_run_carries_on(self) -> None:
        git = GitCli(self.repo.checkout)
        with mock.patch.object(git, "remove_stale_worktrees", side_effect=GitError("boom")), self.assertLogs("conductor", "WARNING"):
            self.forge.add_sub_issues(PRD, [ticket(3)])
            self.agents = FakeAgents(self.behaviors)
            cfg = Config(verify_commands=(self.verify_command(),), parallel=1).with_run(
                prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root, autonomy="halt-each-pr"
            )
            notes_path(cfg).parent.mkdir(parents=True, exist_ok=True)
            notes_path(cfg).write_text("notes")
            result = run(cfg, self.forge, self.agents, git)
        self.assertEqual(result.exit_code, EXIT_OK)


if __name__ == "__main__":
    unittest.main()
