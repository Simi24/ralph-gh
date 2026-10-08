import unittest

from conductor.config import Config
from conductor.exploration import notes_path
from conductor.git_adapter import GitCli
from conductor.ports import Blocker, Issue
from conductor.run import EXIT_INCOMPLETE, EXIT_OK, EXIT_STARTUP_ERROR, run
from tests.fakes import FakeAgents, FakeForge, commits_file, says
from tests.gitrepo import TempRepo

PRD = 52
Q = frozenset({"ralph:queued"})


def ticket(number: int, *extra: str) -> Issue:
    return Issue(number, f"ticket {number}", Q | frozenset(extra))


def in_prd(number: int) -> Blocker:
    return Blocker("", number, "open")


def implement_by_ticket(request):
    return commits_file(f"{request.cwd.name}.txt")(request)


class FrontierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.forge = FakeForge([Issue(PRD, "Graph PRD")])
        self.agents = FakeAgents({"implementer": implement_by_ticket, "ticket-gate": says("GATE:PASS")})

    def start(self, *tickets: Issue):
        self.forge.add_sub_issues(PRD, list(tickets))
        config = Config(verify_commands=("true",)).with_run(
            prd=PRD, repo_root=self.repo.checkout, state_root=self.repo.state_root
        )
        # Exploration is covered in test_exploration.py; here the notes already exist.
        notes = notes_path(config)
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("notes")
        return run(config, self.forge, self.agents, GitCli(self.repo.checkout))

    def order(self) -> list[str]:
        return [r.cwd.name for r in self.agents.requests if r.role == "implementer"]

    def labels(self, number: int) -> frozenset[str]:
        return self.forge.get_issue(number).labels

    def test_linear_chain_is_integrated_in_dependency_order(self) -> None:
        self.forge.blockers = {2: [in_prd(1)], 3: [in_prd(2)]}
        result = self.start(ticket(3), ticket(1), ticket(2))
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.order(), ["ticket-1", "ticket-2", "ticket-3"])
        for n in (1, 2, 3):
            self.assertIn("ralph:integrated", self.labels(n))

    def test_blocker_in_prd_waits_for_integration_not_for_closing(self) -> None:
        # Ticket 1 is integrated by the run but stays open; ticket 2 still runs after it.
        self.forge.blockers = {2: [in_prd(1)]}
        result = self.start(ticket(2), ticket(1))
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.forge.get_issue(1).state, "open")
        self.assertEqual(self.order(), ["ticket-1", "ticket-2"])

    def test_ticket_that_failed_does_not_unblock_dependents(self) -> None:
        self.forge.blockers = {2: [in_prd(1)]}
        self.agents.behaviors["ticket-gate"] = says("GATE:FAIL")
        result = self.start(ticket(1), ticket(2))
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertEqual(self.order(), ["ticket-1"])
        self.assertIn("ralph:queued", self.labels(2))

    def test_open_external_blocker_is_never_dispatched(self) -> None:
        self.forge.blockers = {2: [Blocker("other/repo", 9, "open")]}
        result = self.start(ticket(1), ticket(2))
        self.assertEqual(self.order(), ["ticket-1"])
        self.assertEqual(result.exit_code, EXIT_INCOMPLETE)
        self.assertIn("ralph:queued", self.labels(2))

    def test_closed_external_blocker_lets_the_ticket_run(self) -> None:
        self.forge.blockers = {2: [Blocker("other/repo", 9, "closed")]}
        result = self.start(ticket(2))
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.order(), ["ticket-2"])

    def test_external_blocker_sharing_a_number_with_a_prd_ticket_is_not_confused(self) -> None:
        self.forge.blockers = {2: [Blocker("other/repo", 1, "open")]}
        self.start(Issue(1, "t1", Q, repo="me/repo"), Issue(2, "t2", Q, repo="me/repo"))
        self.assertEqual(self.order(), ["ticket-1"])

    def test_cycle_aborts_before_any_session_naming_the_tickets(self) -> None:
        self.forge.blockers = {1: [in_prd(3)], 2: [in_prd(1)], 3: [in_prd(2)]}
        result = self.start(ticket(1), ticket(2), ticket(3), ticket(4))
        self.assertEqual(result.exit_code, EXIT_STARTUP_ERROR)
        for n in (1, 2, 3):
            self.assertIn(f"#{n}", result.reason)
        self.assertNotIn("#4", result.reason)
        self.assertEqual(self.agents.requests, [])
        self.assertEqual(self.forge.label_trail, {})

    def test_prd_without_sub_issues_aborts_before_any_session(self) -> None:
        result = self.start()
        self.assertEqual(result.exit_code, EXIT_STARTUP_ERROR)
        self.assertEqual(self.agents.requests, [])

    def test_tickets_not_queued_or_already_in_a_ralph_state_are_left_alone(self) -> None:
        result = self.start(
            ticket(1),
            Issue(2, "plain", frozenset({"bug"})),
            Issue(3, "busy", frozenset({"ralph:in-progress"})),
            Issue(4, "done", frozenset({"ralph:integrated"})),
        )
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.order(), ["ticket-1"])
        self.assertEqual(self.labels(2), frozenset({"bug"}))
        self.assertEqual(self.labels(3), frozenset({"ralph:in-progress"}))

    def test_hitl_arch_tickets_come_last_then_ascending_number(self) -> None:
        self.start(ticket(1, "ralph:hitl-arch"), ticket(4), ticket(2), ticket(3, "ralph:hitl-arch"))
        self.assertEqual(self.order(), ["ticket-2", "ticket-4", "ticket-1", "ticket-3"])

    def test_issue_body_text_has_no_effect_on_the_graph(self) -> None:
        # Issue carries no body: the graph is built from native blockers only.
        self.assertNotIn("body", Issue.__dataclass_fields__)
        self.start(ticket(2), ticket(1))
        self.assertEqual(self.order(), ["ticket-1", "ticket-2"])

    def test_unreadable_dependencies_fail_closed(self) -> None:
        def boom(number):
            raise RuntimeError("api down")

        self.forge.list_blockers = boom
        result = self.start(ticket(1))
        self.assertEqual(result.exit_code, EXIT_STARTUP_ERROR)
        self.assertEqual(self.agents.requests, [])


if __name__ == "__main__":
    unittest.main()
