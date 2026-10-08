import unittest

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.git_adapter import GitCli
from conductor.ports import Issue, SessionRequest, SessionResult
from conductor.run import EXIT_OK, run
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
TICKET = 60
NOTES_BODY = "SENTINEL-NOTES-CONTENT: the seams live in ports.py"


def writes_notes(request: SessionRequest) -> SessionResult:
    # The conductor grants the notes directory through add_dirs.
    (request.add_dirs[0] / "exploration.md").write_text(NOTES_BODY)
    return SessionResult(text="mapped\nRALPH:DONE")


class ExplorationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Python conductor rewrite")])
        self.forge.on_merge = self.repo.merge_branch  # the integration branch gets the merged work, which verify checks
        self.forge.add_sub_issues(PRD, [Issue(TICKET, "exploration", frozenset({"ralph:queued"}))])
        self.git = GitCli(self.repo.checkout)
        self.config = Config(verify_commands=("test -f feature.txt",)).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root
        )

    def agents(self, exploration) -> FakeAgents:
        return FakeAgents(
            {
                "exploration": exploration,
                "implementer": commits_file("feature.txt"),
                "ticket-gate": says("ok\nGATE:PASS"),
                "final-review": says("ok\nGATE:PASS"),
            }
        )

    def roles(self, agents: FakeAgents) -> list[str]:
        return [r.role for r in agents.requests]

    def implementer(self, agents: FakeAgents) -> SessionRequest:
        return next(r for r in agents.requests if r.role == "implementer")

    def test_exploration_runs_once_before_the_first_implementer_and_writes_outside_the_repo(self) -> None:
        agents = self.agents(writes_notes)
        result = run(self.config, self.forge, agents, self.git)

        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.roles(agents)[:2], ["exploration", "implementer"])
        self.assertEqual(self.roles(agents).count("exploration"), 1)
        path = notes_path(self.config)
        self.assertTrue(path.is_file())
        self.assertFalse(path.is_relative_to(self.repo.checkout))
        self.assertTrue(path.is_relative_to(self.repo.state_root))

    def test_implementer_prompt_has_the_path_but_not_the_content(self) -> None:
        agents = self.agents(writes_notes)
        run(self.config, self.forge, agents, self.git)

        implementer = self.implementer(agents)
        self.assertIn(str(notes_path(self.config)), implementer.prompt)
        self.assertNotIn(NOTES_BODY, implementer.prompt)
        self.assertIn(notes_path(self.config).parent, implementer.add_dirs)

    def test_existing_notes_skip_the_exploration_session(self) -> None:
        path = notes_path(self.config)
        path.parent.mkdir(parents=True)
        path.write_text(NOTES_BODY)
        agents = self.agents(says("must not run"))

        run(self.config, self.forge, agents, self.git)

        self.assertNotIn("exploration", self.roles(agents))
        self.assertIn(str(path), self.implementer(agents).prompt)

    def assert_run_continues_without_notes(self, exploration) -> None:
        agents = self.agents(exploration)
        with self.assertLogs("conductor", level="WARNING") as logs:
            result = run(self.config, self.forge, agents, self.git)

        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertIn("exploration", "\n".join(logs.output))
        implementer = self.implementer(agents)
        self.assertNotIn("exploration notes", implementer.prompt)
        self.assertNotIn("exploration.md", implementer.prompt)
        self.assertEqual(implementer.add_dirs, ())
        self.assertFalse(notes_path(self.config).exists())

    def test_blocked_exploration_does_not_stop_the_run(self) -> None:
        self.assert_run_continues_without_notes(says("RALPH:BLOCKED no access"))

    def test_done_without_a_notes_file_does_not_stop_the_run(self) -> None:
        self.assert_run_continues_without_notes(says("RALPH:DONE"))

    def test_timed_out_exploration_with_partial_notes_is_discarded(self) -> None:
        def partial(request: SessionRequest) -> SessionResult:
            (request.add_dirs[0] / "exploration.md").write_text("half")
            return SessionResult(text="RALPH:DONE", timed_out=True, returncode=-9)

        self.assert_run_continues_without_notes(partial)


if __name__ == "__main__":
    unittest.main()
