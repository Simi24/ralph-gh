import subprocess
import unittest

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.findings import blocking_findings
from conductor.git_adapter import GitCli
from conductor.ports import Blocker, Issue, SessionResult
from conductor.run import EXIT_INCOMPLETE, EXIT_OK, run
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
Q = frozenset({"ralph:queued"})
VERDICT = """FAIL
### Follow-up findings
- FOLLOWUP-SENTINEL rename a helper
### Blocking findings
- BLOCKING-SENTINEL src/a.py:3 off by one
GATE:FAIL"""


def ticket(number: int) -> Issue:
    return Issue(number, f"ticket {number}", Q)


def sequence(*results: str):
    """Behavior answering with each text in turn, then repeating the last one."""
    texts = list(results)

    def behavior(request):
        return SessionResult(text=texts.pop(0) if len(texts) > 1 else texts[0])

    return behavior


def implement_by_ticket(request):
    return commits_file(f"{request.cwd.name}.txt")(request)


def fix_with_file(name: str):
    return commits_file(name, "fixed\nRALPH:DONE")


class FixLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Fix loops")])
        self.behaviors = {
            "implementer": implement_by_ticket,
            "ticket-gate": says("GATE:PASS"),
            "fix": says("RALPH:DONE"),
        }

    def start(self, *tickets: Issue, verify: str = "true", rounds: int = 2):
        self.forge.add_sub_issues(PRD, list(tickets))
        self.agents = FakeAgents(self.behaviors)
        config = Config(verify_commands=(verify,), gate_fix_rounds=rounds).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root
        )
        notes = notes_path(config)
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("notes")
        self.notes = notes
        return run(config, self.forge, self.agents, GitCli(self.repo.checkout))

    def labels(self, number: int) -> frozenset[str]:
        return self.forge.get_issue(number).labels

    def roles(self) -> list[str]:
        return [r.role for r in self.agents.requests]

    def test_failing_verify_is_repaired_by_a_fix_session_and_integrated(self) -> None:
        self.behaviors["fix"] = fix_with_file("fixed.txt")
        result = self.start(ticket(1), verify="test -f fixed.txt")
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertIn("ralph:integrated", self.labels(1))
        self.assertEqual(self.roles(), ["implementer", "fix", "ticket-gate"])
        self.assertIn("test -f fixed.txt", self.agents.requests[1].prompt)  # the failing output

    def test_gate_fail_fix_receives_only_blocking_findings_then_regate_integrates(self) -> None:
        self.behaviors["ticket-gate"] = sequence(VERDICT, "GATE:PASS")
        self.behaviors["fix"] = fix_with_file("fixed.txt")
        result = self.start(ticket(1))
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.roles(), ["implementer", "ticket-gate", "fix", "ticket-gate"])
        fix = self.agents.requests[2]
        self.assertIn("BLOCKING-SENTINEL", fix.prompt)
        self.assertNotIn("FOLLOWUP-SENTINEL", fix.prompt)
        self.assertIn(str(self.notes), fix.prompt)  # notes pointer
        self.assertEqual(fix.add_dirs, (self.notes.parent,))
        (pr,) = self.forge.prs.values()
        self.assertEqual(pr["state"], "merged")

    def test_verify_fix_prompt_carries_the_notes_pointer(self) -> None:
        self.behaviors["fix"] = fix_with_file("fixed.txt")
        self.start(ticket(1), verify="test -f fixed.txt")
        fix = self.agents.requests[1]
        self.assertIn(str(self.notes), fix.prompt)
        self.assertEqual(fix.add_dirs, (self.notes.parent,))

    def test_exhausted_gate_rounds_fail_the_ticket_and_leave_the_pr_open(self) -> None:
        self.behaviors["ticket-gate"] = says(VERDICT)
        result = self.start(ticket(1), rounds=2)
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.roles().count("fix"), 2)
        self.assertEqual(self.roles().count("ticket-gate"), 3)
        self.assertIn("ralph:failed:issue", self.labels(1))
        self.assertNotIn("ralph:in-review", self.labels(1))
        self.assertIn("2 fix round", self.forge.comments[1][0])
        (pr,) = self.forge.prs.values()
        self.assertEqual(pr["state"], "open")
        self.assertEqual(self.forge.merges, [])

    def test_exhausted_verify_rounds_fail_the_ticket_with_the_output_in_the_comment(self) -> None:
        result = self.start(ticket(1), verify="echo VERIFY-SENTINEL; false", rounds=1)
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.roles(), ["implementer", "fix"])
        self.assertIn("ralph:failed:issue", self.labels(1))
        self.assertIn("VERIFY-SENTINEL", self.forge.comments[1][0])

    def test_failed_ticket_does_not_stop_independent_tickets(self) -> None:
        def gate(request):
            if request.cwd.name == "ticket-1":
                return SessionResult(text="GATE:FAIL")
            return SessionResult(text="GATE:PASS")

        self.behaviors["ticket-gate"] = gate
        result = self.start(ticket(1), ticket(2), rounds=0)
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertIn("ralph:failed:issue", self.labels(1))
        self.assertIn("ralph:integrated", self.labels(2))

    def test_cascade_when_every_remaining_ticket_depends_on_a_failed_one(self) -> None:
        self.forge.blockers = {2: [Blocker("", 1, "open")], 3: [Blocker("", 2, "open")]}
        self.behaviors["ticket-gate"] = says("GATE:FAIL")
        result = self.start(ticket(1), ticket(2), ticket(3), rounds=0)
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertIn("cascade", result.reason)
        self.assertIn("#1", result.reason)
        self.assertIn("#2", result.reason)
        self.assertIn("#3", result.reason)
        self.assertIn("ralph:queued", self.labels(2))
        self.assertEqual(self.roles().count("implementer"), 1)

    def test_blocked_implementer_is_labelled_blocked_with_its_reason(self) -> None:
        self.behaviors["implementer"] = says("RALPH:BLOCKED need a schema decision")
        result = self.start(ticket(1))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertIn("ralph:blocked", self.labels(1))
        self.assertNotIn("ralph:failed:issue", self.labels(1))
        self.assertIn("need a schema decision", self.forge.comments[1][0])

    def test_blocked_fixer_is_an_escalation_not_a_failure(self) -> None:
        self.behaviors["ticket-gate"] = says(VERDICT)
        self.behaviors["fix"] = says("RALPH:BLOCKED contradicting findings")
        result = self.start(ticket(1))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertIn("ralph:blocked", self.labels(1))
        self.assertNotIn("ralph:failed:issue", self.labels(1))
        self.assertIn("contradicting findings", self.forge.comments[1][0])
        self.assertEqual(self.roles().count("fix"), 1)

    def test_cascade_names_a_blocked_ticket_too(self) -> None:
        self.forge.blockers = {2: [Blocker("", 1, "open")]}
        self.behaviors["implementer"] = says("RALPH:BLOCKED nope")
        result = self.start(ticket(1), ticket(2))
        self.assertIn("cascade", result.reason)
        self.assertIn("#1", result.reason)

    def test_unparsable_verdict_is_retried_once_with_a_fresh_session(self) -> None:
        self.behaviors["ticket-gate"] = sequence("no idea", "GATE:PASS")
        result = self.start(ticket(1))
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.roles(), ["implementer", "ticket-gate", "ticket-gate"])
        self.assertEqual(self.roles().count("fix"), 0)

    def test_second_unparsable_verdict_fails_and_is_never_a_pass(self) -> None:
        self.behaviors["ticket-gate"] = says("looks great, GATE:PASS I think")
        result = self.start(ticket(1))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.roles().count("ticket-gate"), 2)
        self.assertEqual(self.forge.merges, [])
        self.assertIn("ralph:failed:issue", self.labels(1))
        self.assertIn("never judged", self.forge.comments[1][0])

    def test_timed_out_gate_is_not_retried(self) -> None:
        self.behaviors["ticket-gate"] = lambda r: SessionResult(text="GATE:PASS", timed_out=True)
        self.start(ticket(1))
        self.assertEqual(self.roles().count("ticket-gate"), 1)
        self.assertEqual(self.forge.merges, [])

    def test_failed_ticket_branch_is_pushed_for_the_human(self) -> None:
        self.behaviors["ticket-gate"] = says("GATE:FAIL")
        self.start(ticket(1), rounds=0)
        self.repo.git("fetch", "origin")
        out = subprocess.run(
            ["git", "branch", "-r", "--list", "origin/feat/52-ticket-1"],
            cwd=self.repo.checkout, capture_output=True, text=True,
        ).stdout
        self.assertIn("origin/feat/52-ticket-1", out)


class BlockingFindingsTest(unittest.TestCase):
    def test_extracts_the_section_without_the_marker(self) -> None:
        text, found = blocking_findings(VERDICT)
        self.assertTrue(found)
        self.assertIn("BLOCKING-SENTINEL", text)
        self.assertNotIn("FOLLOWUP-SENTINEL", text)
        self.assertNotIn("GATE:FAIL", text)

    def test_missing_or_none_section_falls_back_to_the_verdict_body(self) -> None:
        for verdict in ("FAIL\nAC 2 unmet\nGATE:FAIL", "FAIL\nAC 2 unmet\n### Blocking findings\nnone\nGATE:FAIL"):
            text, found = blocking_findings(verdict)
            self.assertFalse(found)
            self.assertIn("AC 2 unmet", text)


if __name__ == "__main__":
    unittest.main()
