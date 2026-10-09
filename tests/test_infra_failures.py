"""A git or gh failure inside one ticket ends that ticket as `ralph:failed:systemic`
(never a traceback), a refused merge that actually merged is integrated, and a
ticket whose PR a human closed unmerged is redone from a deleted stale branch."""
import unittest

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.gh_runner import GhError
from conductor.git_adapter import GitCli, GitError
from conductor.naming import integration_branch
from conductor.ports import Blocker, Issue
from conductor.run import EXIT_INCOMPLETE, EXIT_OK, run
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
PRD_TITLE = "Infra PRD"
INTEGRATION = integration_branch("feat", PRD, PRD_TITLE)
Q = frozenset({"ralph:queued"})


def implement_by_ticket(request):
    return commits_file(f"{request.cwd.name}.txt")(request)


class BrokenCreatePrForge(FakeForge):
    def create_pr(self, **kwargs):
        if kwargs["head"] == "feat/52-ticket-1":
            raise GhError("gh pr create: HTTP 502")
        return super().create_pr(**kwargs)


class MergesButReportsFalseForge(FakeForge):
    def merge_pr(self, number, *, method, head_sha):
        super().merge_pr(number, method=method, head_sha=head_sha)
        return False  # gh died after the merge went through


class FailingPushGit(GitCli):
    def push(self, path, branch):
        if branch == "feat/52-ticket-1":
            raise GitError("git push: remote hung up")
        super().push(path, branch)


class UndeletableBranchGit(GitCli):
    def delete_remote_branch(self, branch):
        raise GitError("git push origin :ref: permission denied")


class InfraFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.agents = FakeAgents(
            {
                "implementer": implement_by_ticket,
                "ticket-gate": says("GATE:PASS"),
                "fix": says("RALPH:DONE"),
                "final-review": says("GATE:PASS"),
            }
        )

    def forge(self, cls=FakeForge) -> FakeForge:
        forge = cls([Issue(PRD, PRD_TITLE)])
        forge.add_sub_issues(PRD, [Issue(1, "ticket 1", Q), Issue(2, "ticket 2", Q)])
        return forge

    def start(self, forge: FakeForge, git: GitCli | None = None):
        config = Config(verify_commands=("true",)).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root, autonomy="halt-each-pr"
        )
        notes = notes_path(config)
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("notes")
        return run(config, forge, self.agents, git or GitCli(self.repo.checkout))

    def assert_systemic_and_sibling_integrated(self, forge: FakeForge, result) -> None:
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(forge.get_issue(1).labels, frozenset({"ralph:failed:systemic"}))
        self.assertTrue(any("failed" in body for _, body in forge.comments.get(1, [])))
        self.assertIn("ralph:integrated", forge.get_issue(2).labels)
        self.assertFalse((self.repo.state_root / f"prd-{PRD}" / "worktrees" / "ticket-1").exists())

    def test_a_gh_failure_fails_that_ticket_as_systemic_while_a_sibling_integrates(self) -> None:
        forge = self.forge(BrokenCreatePrForge)
        self.assert_systemic_and_sibling_integrated(forge, self.start(forge))

    def test_a_git_failure_fails_that_ticket_as_systemic_while_a_sibling_integrates(self) -> None:
        forge = self.forge()
        git = FailingPushGit(self.repo.checkout)
        self.assert_systemic_and_sibling_integrated(forge, self.start(forge, git))

    def test_a_systemic_failure_halts_dependents_like_a_failed_ticket(self) -> None:
        forge = self.forge(BrokenCreatePrForge)
        forge.add_sub_issues(PRD, [Issue(1, "ticket 1", Q), Issue(2, "ticket 2", Q)])
        forge.blockers = {2: [Blocker("", 1, "open")]}
        result = self.start(forge)
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertIn("cascade", result.reason)
        self.assertIn("ralph:queued", forge.get_issue(2).labels)

    def test_a_merge_reported_refused_but_merged_ends_integrated(self) -> None:
        forge = self.forge(MergesButReportsFalseForge)
        result = self.start(forge)
        self.assertEqual(result.exit_code, EXIT_OK)
        for number in (1, 2):
            self.assertNotIn("ralph:failed:systemic", forge.get_issue(number).labels)
            self.assertNotIn("ralph:failed:issue", forge.get_issue(number).labels)


class ClosedPrRedoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.agents = FakeAgents(
            {
                "implementer": commits_file("feature.txt"),
                "ticket-gate": says("GATE:PASS"),
                "fix": says("RALPH:DONE"),
                "final-review": says("GATE:PASS"),
            }
        )
        self.forge = FakeForge([Issue(PRD, PRD_TITLE)])
        self.forge.add_sub_issues(PRD, [Issue(1, "ticket 1", Q)])
        self.repo.git("checkout", "-b", "stale", "main")
        (self.repo.checkout / "stale.txt").write_text("old attempt\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-m", "feat: stale attempt")
        self.repo.git("push", "origin", "stale:refs/heads/feat/52-ticket-1")
        self.repo.git("checkout", "main")
        self.stale_sha = self.repo.git("rev-parse", "stale")
        self.forge.prs[101] = {
            "head": "feat/52-ticket-1", "base": INTEGRATION, "title": "t", "body": "", "state": "closed", "draft": False,
        }

    def start(self, git: GitCli):
        config = Config(verify_commands=("true",)).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root, autonomy="halt-each-pr"
        )
        notes = notes_path(config)
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("notes")
        return run(config, self.forge, self.agents, git)

    def remote_files(self) -> str:
        return self.repo.git("ls-tree", "--name-only", "feat/52-ticket-1", cwd=self.repo.origin)

    def test_a_redone_ticket_deletes_the_stale_remote_branch_and_pushes_fresh(self) -> None:
        result = self.start(GitCli(self.repo.checkout))
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertIn("ralph:integrated", self.forge.get_issue(1).labels)
        self.assertIn("feature.txt", self.remote_files())
        self.assertNotIn("stale.txt", self.remote_files())

    def test_a_failed_branch_deletion_is_systemic_and_never_a_force_push(self) -> None:
        result = self.start(UndeletableBranchGit(self.repo.checkout))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.forge.get_issue(1).labels, frozenset({"ralph:failed:systemic"}))
        self.assertEqual(self.repo.git("rev-parse", "feat/52-ticket-1", cwd=self.repo.origin), self.stale_sha)
        self.assertEqual(self.agents.requests, [])  # nothing was implemented


if __name__ == "__main__":
    unittest.main()
