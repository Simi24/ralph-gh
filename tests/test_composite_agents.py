"""Unit tests for CompositeAgents router."""
import unittest
from pathlib import Path

from conductor.composite_agents import CompositeAgents
from conductor.ports import SessionRequest, SessionResult


class RecordingAgents:
    def __init__(self, name: str) -> None:
        self.name = name
        self.requests: list[SessionRequest] = []

    def start(self, request: SessionRequest) -> SessionResult:
        self.requests.append(request)
        return SessionResult(text=f"ok from {self.name}", returncode=0)


class CompositeAgentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.claude = RecordingAgents("claude")
        self.antigravity = RecordingAgents("antigravity")
        self.engines = {"claude": self.claude, "antigravity": self.antigravity}

    def req(self, role: str, **kwargs: object) -> SessionRequest:
        return SessionRequest(role=role, prompt="test", cwd=Path("/tmp"), **kwargs)  # type: ignore[arg-type]

    def test_routes_writing_roles_to_writer_engine(self) -> None:
        router = CompositeAgents(self.engines, writer_engine="antigravity", reviewer_engine="claude")
        for role in ("implementer", "fix", "merge-fix", "integration-fix", "final-fix", "exploration"):
            with self.subTest(role):
                res = router.start(self.req(role))
                self.assertEqual(res.text, "ok from antigravity")
                self.assertEqual(self.antigravity.requests[-1].role, role)

    def test_routes_review_roles_to_reviewer_engine(self) -> None:
        router = CompositeAgents(self.engines, writer_engine="antigravity", reviewer_engine="claude")
        for role in ("ticket-gate", "final-review"):
            with self.subTest(role):
                res = router.start(self.req(role))
                self.assertEqual(res.text, "ok from claude")
                self.assertEqual(self.claude.requests[-1].role, role)

    def test_applies_writer_and_reviewer_models(self) -> None:
        router = CompositeAgents(
            self.engines,
            writer_engine="antigravity",
            reviewer_engine="claude",
            writer_model="gemini-flash",
            reviewer_model="claude-opus",
        )
        # Writer role gets writer_model
        router.start(self.req("implementer"))
        self.assertEqual(self.antigravity.requests[-1].model, "gemini-flash")

        # Review role gets reviewer_model
        router.start(self.req("ticket-gate"))
        self.assertEqual(self.claude.requests[-1].model, "claude-opus")

        # Review role with agent also gets reviewer_model applied
        router.start(self.req("final-review", agent="ralph-guard:ralph-gate-reviewer"))
        self.assertEqual(self.claude.requests[-1].model, "claude-opus")

        # Explicit request model is preserved
        router.start(self.req("implementer", model="custom-model"))
        self.assertEqual(self.antigravity.requests[-1].model, "custom-model")

    def test_unknown_engine_raises_value_error(self) -> None:
        router = CompositeAgents(self.engines, writer_engine="nonexistent")
        with self.assertRaises(ValueError) as ctx:
            router.start(self.req("implementer"))
        self.assertIn("nonexistent", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
