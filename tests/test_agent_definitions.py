"""The shipped agent definitions: both reviewers resolve by name, pin a model, and end in a GATE marker."""
import re
import unittest
from pathlib import Path

from conductor.config import parse_config
from conductor.preflight import find_agent

AGENTS = Path(__file__).resolve().parent.parent / "worker-bundle" / "agents"


class AgentDefinitionsTest(unittest.TestCase):
    def test_default_agents_exist_and_pin_a_model(self) -> None:
        config = parse_config({"verify_commands": ["true"]})
        for name in (config.reviewer_agent, config.ticket_gate_agent):
            with self.subTest(name):
                path = find_agent(name.rpartition(":")[2], [AGENTS])
                self.assertIsNotNone(path)
                text = path.read_text()  # type: ignore[union-attr]
                self.assertRegex(text, r"(?m)^model:\s*\S+$")
                self.assertIn("GATE:PASS", text)
                self.assertIn("GATE:FAIL", text)

    def test_final_reviewer_keeps_its_protocol(self) -> None:
        text = (AGENTS / "ralph-gate-reviewer.md").read_text()
        for needle in ("Tier 1", "Tier 2", "Tier 3", "Repo override", "Re-gate mode", "[BLOCKING]", "[FOLLOW-UP]"):
            self.assertIn(needle, text)

    def test_ticket_gate_is_spec_only(self) -> None:
        text = (AGENTS / "ralph-ticket-gate.md").read_text()
        self.assertIn("acceptance criteria", text)
        self.assertNotRegex(text, re.compile(r"Tier [123]"))

    def test_refactorer_is_gone(self) -> None:
        self.assertFalse((AGENTS / "ralph-refactorer.md").exists())


if __name__ == "__main__":
    unittest.main()
