"""A ticket PR that no longer merges cleanly goes to a merge-fix session, is
re-verified, and is merged; a conflict alone never fails a ticket."""
import subprocess
import unittest

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.git_adapter import GitCli
from conductor.ports import Issue, SessionResult
from conductor.run import EXIT_INCOMPLETE, EXIT_OK, run
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
BRANCH = "feat/52-ticket-1"
INTEGRATION = "feat/52-merge-fix-prd"


class MergeFixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.moves = 0
        self.verify_log = self.repo.root / "verify.log"
        self.forge = FakeForge([Issue(PRD, "Merge fix PRD")])
        self.forge.add_sub_issues(PRD, [Issue(1, "ticket 1", frozenset({"ralph:queued"}))])
        self.forge.refuse_merge[BRANCH] = 1  # the first merge hits a conflict
        self.behaviors = {
            "implementer": commits_file("shared.txt"),
            "ticket-gate": self.gate_moves_the_tip,
            "final-review": says("GATE:PASS"),
            "merge-fix": self.merge_tip,
            "fix": says("RALPH:DONE"),
        }

    def gate_moves_the_tip(self, request) -> SessionResult:
        """While the ticket is in review another ticket lands: shared.txt changes on the integration branch."""
        git = self.repo.git
        self.moves += 1
        git("fetch", "origin")
        git("checkout", "-B", "tip", f"origin/{INTEGRATION}")
        (self.repo.checkout / "shared.txt").write_text(f"from another ticket {self.moves}\n")
        git("add", "-A")
        git("commit", "-m", "feat: another ticket")
        git("push", "origin", f"tip:{INTEGRATION}")
        git("checkout", "main")
        return SessionResult(text="GATE:PASS")

    def merge_tip(self, request) -> SessionResult:
        cwd = request.cwd
        run_git = lambda *a, check=True: subprocess.run(["git", *a], cwd=cwd, check=check, capture_output=True)
        run_git("fetch", "origin")
        run_git("merge", f"origin/{INTEGRATION}", check=False)  # conflicts in shared.txt
        (cwd / "shared.txt").write_text("resolved\n")
        run_git("add", "-A")
        run_git("commit", "--no-edit")
        return SessionResult(text="merged\nRALPH:DONE")

    def start(self, rounds: int = 2):
        self.agents = FakeAgents(self.behaviors)
        config = Config(
            verify_commands=(f"echo run >> {self.verify_log}",), gate_fix_rounds=rounds
        ).with_run(prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root, autonomy="halt-each-pr")
        notes = notes_path(config)
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("notes")
        return run(config, self.forge, self.agents, GitCli(self.repo.checkout))

    def roles(self) -> list[str]:
        return [r.role for r in self.agents.requests]

    def test_a_conflict_goes_to_merge_fix_then_reverify_then_merge(self) -> None:
        result = self.start()
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.roles().count("merge-fix"), 1)
        self.assertEqual(self.roles().count("ticket-gate"), 2)  # gated again after the merge-fix
        self.assertEqual(self.roles().index("merge-fix") + 1, self.roles().index("ticket-gate", self.roles().index("merge-fix")))
        self.assertEqual(len(self.verify_log.read_text().splitlines()), 2)  # before the first gate, after the merge-fix
        self.assertEqual(self.forge.get_issue(1).labels & {"ralph:failed:issue"}, frozenset())
        self.assertIn("ralph:integrated", self.forge.get_issue(1).labels)
        # The second merge is pinned to the merged head, which contains the tip.
        self.assertEqual(len(self.forge.merges), 1)
        head = self.repo.git("rev-parse", BRANCH, cwd=self.repo.origin)
        self.assertEqual(self.forge.merges[0][2], head)

    def gates(self) -> list[str]:
        return [r.prompt for r in self.agents.requests if r.role == "ticket-gate"]

    def test_every_gate_carries_the_scope_rule_and_only_the_regate_names_the_resolution(self) -> None:
        self.start()
        first, second = self.gates()
        for prompt in (first, second):
            self.assertIn("must contain only this ticket's", prompt)
            self.assertIn("is BLOCKING", prompt)
        self.assertNotIn("merge conflict", first)
        head = self.repo.git("rev-parse", BRANCH, cwd=self.repo.origin)
        self.assertIn("A merge conflict", second)
        self.assertIn(f"merge commit\n`{head}`", second)  # the merge-fix left HEAD on the merge commit

    def scripted_gate(self, *later: str):
        """First gate: PASS, and another ticket lands (a conflict). Then the given verdicts, the last one repeating."""
        verdicts = list(later)

        def gate(request) -> SessionResult:
            if not self.moves:
                return self.gate_moves_the_tip(request)
            return SessionResult(text=verdicts.pop(0) if len(verdicts) > 1 else verdicts[0])

        return gate

    def test_a_gate_fail_after_a_merge_fix_goes_through_the_gate_fix_rounds(self) -> None:
        self.behaviors["ticket-gate"] = self.scripted_gate("### Blocking findings\n- dropped shared.txt\nGATE:FAIL", "GATE:PASS")
        result = self.start()
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.roles().count("fix"), 1)
        self.assertEqual(self.roles().count("ticket-gate"), 3)  # first, after merge-fix (FAIL), after the fix
        self.assertIn("dropped shared.txt", [r for r in self.agents.requests if r.role == "fix"][0].prompt)
        self.assertIn("ralph:integrated", self.forge.get_issue(1).labels)

    def test_a_gate_that_keeps_failing_after_a_merge_fix_fails_the_ticket_after_the_same_rounds(self) -> None:
        self.behaviors["ticket-gate"] = self.scripted_gate("### Blocking findings\n- bad\nGATE:FAIL")
        result = self.start(rounds=2)
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.roles().count("fix"), 2)
        self.assertEqual(self.forge.merges, [])
        self.assertIn("ralph:failed:issue", self.forge.get_issue(1).labels)

    def test_merge_fix_attempts_are_bounded_separately_from_gate_fix_rounds(self) -> None:
        self.forge.refuse_merge[BRANCH] = 99  # never merges
        self.behaviors["merge-fix"] = self.merge_tip_again
        result = self.start(rounds=2)
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.roles().count("merge-fix"), 2)
        self.assertIn("ralph:failed:issue", self.forge.get_issue(1).labels)

    def merge_tip_again(self, request) -> SessionResult:
        """Each attempt merges the tip, then yet another ticket lands."""
        outcome = self.merge_tip(request)
        self.gate_moves_the_tip(request)
        return outcome

    def test_a_refused_merge_that_is_not_a_conflict_fails_without_merge_fix(self) -> None:
        # Nothing moved on the integration branch: the refusal has another cause.
        self.behaviors["ticket-gate"] = says("GATE:PASS")
        result = self.start()
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.roles().count("merge-fix"), 0)
        self.assertIn("ralph:failed:systemic", self.forge.get_issue(1).labels)
        self.assertNotIn("ralph:failed:issue", self.forge.get_issue(1).labels)


if __name__ == "__main__":
    unittest.main()
