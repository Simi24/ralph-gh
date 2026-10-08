import unittest

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.git_adapter import GitCli
from conductor.ports import Issue
from conductor.run import EXIT_INCOMPLETE, EXIT_OK, run
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
TICKET = 54
INTEGRATION = "feat/52-python-conductor-rewrite"
TICKET_TITLE = "SENTINEL-TICKET-TITLE tracer bullet"


class TracerBulletTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Python conductor rewrite!")])
        self.forge.add_sub_issues(PRD, [Issue(TICKET, TICKET_TITLE, frozenset({"ralph:queued", "bug"}))])
        self.git = GitCli(self.repo.checkout)
        # Exploration is covered in test_exploration.py; here the notes already exist.
        notes = notes_path(self.config())
        notes.parent.mkdir(parents=True)
        notes.write_text("notes")

    def config(self, verify: str = "test -f feature.txt") -> Config:
        return Config(verify_commands=(verify,)).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root
        )

    def agents(self, implementer=None, gate=None) -> FakeAgents:
        return FakeAgents(
            {
                "implementer": implementer or commits_file("feature.txt"),
                "ticket-gate": gate or says("criteria met\nGATE:PASS"),
            }
        )

    def labels(self) -> frozenset[str]:
        return self.forge.get_issue(TICKET).labels

    def test_one_ticket_goes_from_queued_to_integrated(self) -> None:
        agents = self.agents()
        result = run(self.config(), self.forge, agents, self.git)

        self.assertEqual(result.exit_code, EXIT_OK)
        (pr_number, pr), = self.forge.prs.items()
        self.assertEqual(pr["state"], "merged")
        self.assertEqual(pr["base"], INTEGRATION)
        self.assertEqual(self.forge.merges[0][:2], (pr_number, "merge"))
        self.assertEqual(self.labels(), frozenset({"ralph:integrated", "bug"}))
        self.assertEqual(
            self.forge.ralph_trail(TICKET),
            [["ralph:queued"], ["ralph:in-progress"], ["ralph:in-review"], ["ralph:integrated"]],
        )

    def test_integration_branch_is_created_from_base(self) -> None:
        run(self.config(), self.forge, self.agents(), self.git)
        self.repo.git("fetch", "origin")
        base = self.repo.git("rev-parse", "origin/main")
        self.assertEqual(self.repo.git("merge-base", "origin/main", f"origin/{INTEGRATION}"), base)

    def test_ticket_pr_is_opened_by_the_conductor_and_references_the_ticket(self) -> None:
        run(self.config(), self.forge, self.agents(), self.git)
        (pr,) = self.forge.prs.values()
        self.assertEqual(pr["head"], "feat/52-ticket-54")
        self.assertIn(f"#{TICKET}", pr["body"])

    def test_merge_is_pinned_to_the_pushed_head(self) -> None:
        run(self.config(), self.forge, self.agents(), self.git)
        self.repo.git("fetch", "origin")
        pushed = self.repo.git("rev-parse", "origin/feat/52-ticket-54")
        self.assertEqual(self.forge.merges[0][2], pushed)

    def test_implementer_runs_in_a_worktree_outside_the_checkout_and_leaves_it_untouched(self) -> None:
        agents = self.agents()
        run(self.config(), self.forge, agents, self.git)
        cwd = agents.requests[0].cwd
        self.assertFalse(cwd.is_relative_to(self.repo.checkout))
        self.assertTrue(cwd.is_relative_to(self.repo.state_root))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertEqual(self.repo.git("branch", "--format=%(refname:short)"), "main")

    def test_prompts_are_pointers_not_issue_text(self) -> None:
        agents = self.agents()
        run(self.config(), self.forge, agents, self.git)
        implementer = agents.requests[0]
        self.assertIn(f"#{TICKET}", implementer.prompt)
        self.assertIn(f"#{PRD}", implementer.prompt)
        self.assertIn(INTEGRATION, implementer.prompt)
        self.assertIn("test -f feature.txt", implementer.prompt)
        self.assertNotIn("SENTINEL", implementer.prompt)
        self.assertIsNone(implementer.agent)

    def test_gate_is_a_reviewer_session_with_the_configured_agent_and_no_model(self) -> None:
        agents = self.agents()
        run(self.config(), self.forge, agents, self.git)
        gate = agents.requests[1]
        self.assertEqual((gate.role, gate.agent, gate.model), ("ticket-gate", "ralph-ticket-gate", None))

    def test_failing_verify_blocks_the_merge(self) -> None:
        result = run(self.config(verify="false"), self.forge, self.agents(), self.git)
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.forge.merges, [])
        self.assertEqual(self.forge.prs, {})
        self.assertIn("ralph:failed:issue", self.labels())

    def test_gate_fail_or_unparsable_never_merges(self) -> None:
        for name, text in [("fail", "GATE:FAIL"), ("unparsable", "looks fine to me"), ("quoted", 'say "GATE:PASS" later')]:
            with self.subTest(name):
                forge = FakeForge([Issue(PRD, "Python conductor rewrite!")])
                forge.add_sub_issues(PRD, [Issue(TICKET, "t", frozenset({"ralph:queued"}))])
                repo = TempRepo()
                self.addCleanup(repo.cleanup)
                config = Config(verify_commands=("true",)).with_run(
                    prd=PRD, repo_root=repo.checkout, state_root=repo.state_root
                )
                notes = notes_path(config)
                notes.parent.mkdir(parents=True)
                notes.write_text("notes")
                agents = FakeAgents({"implementer": commits_file("feature.txt"), "ticket-gate": says(text)})
                result = run(config, forge, agents, GitCli(repo.checkout))
                self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
                self.assertEqual(forge.merges, [])
                self.assertIn("ralph:failed:issue", forge.get_issue(TICKET).labels)

    def test_blocked_implementer_is_an_escalation_not_a_failure(self) -> None:
        agents = self.agents(implementer=says("stuck\nRALPH:BLOCKED need a decision"))
        result = run(self.config(), self.forge, agents, self.git)
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.forge.prs, {})
        self.assertIn("ralph:blocked", self.labels())
        self.assertNotIn("ralph:failed:issue", self.labels())

    def test_prd_without_a_queued_ticket_is_refused(self) -> None:
        self.forge.set_labels(TICKET, remove=("ralph:queued",))
        result = run(self.config(), self.forge, self.agents(), self.git)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.forge.prs, {})


if __name__ == "__main__":
    unittest.main()
