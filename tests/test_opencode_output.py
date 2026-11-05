"""Table test for the OpenCode adapter's output parsing and usage-limit detection."""
import json
import unittest

from conductor.opencode_output import parse_session
from conductor.usage_limit import UNKNOWN_RESET


def text(message: str, body: str) -> dict[str, object]:
    return {"type": "text", "part": {"type": "text", "messageID": message, "text": body}}


def tool(message: str) -> dict[str, object]:
    return {"type": "tool_use", "part": {"type": "tool", "messageID": message, "tool": "bash", "state": {"status": "completed"}}}


def error(message: str, **data: object) -> dict[str, object]:
    return {"type": "error", "error": {"name": "APIError", "data": {"message": message, **data}}}


def events(*items: dict[str, object]) -> str:
    return "\n".join(json.dumps(item) for item in items) + "\n"


LIMIT = "Rate limit exceeded. Your limit resets in 2 hours."


class OpencodeParseSessionTest(unittest.TestCase):
    def test_text_and_flags(self) -> None:
        table = [
            ("one message", events(text("m1", "done\nRALPH:DONE")), "", 0, False, ("done\nRALPH:DONE", False, "")),
            ("last message wins", events(text("m1", "working"), tool("m1"), text("m2", "GATE:PASS")), "", 0, False, ("GATE:PASS", False, "")),
            ("parts of the last message are joined", events(text("m2", "first"), text("m2", "GATE:FAIL")), "", 0, False, ("first\nGATE:FAIL", False, "")),
            ("a final tool step keeps the last text", events(text("m1", "RALPH:DONE"), tool("m2")), "", 0, False, ("RALPH:DONE", False, "")),
            ("no JSON", "oops", "boom", 1, False, ("", False, "")),
            ("empty stdout", "", "", 0, False, ("", False, "")),
            ("garbage lines are skipped", "not json\n" + events(text("m1", "ok")), "", 0, False, ("ok", False, "")),
            ("text that is not a string", events({"type": "text", "part": {"messageID": "m1", "text": None}}), "", 0, False, ("", False, "")),
            ("rate limit error", events(error(LIMIT)), "", 1, False, ("", True, "resets in 2 hours")),
            ("http 429", events(error("Too Many Requests", statusCode=429)), "", 1, False, ("", True, UNKNOWN_RESET)),
            ("usage limit wording", events(error("You have reached your usage limit for this period")), "", 1, False, ("", True, UNKNOWN_RESET)),
            ("limit only in stderr with an error event", events(error("request failed")), LIMIT, 1, False, ("", True, "resets in 2 hours")),
            ("other error is not a limit", events(error("Unexpected server error")), "", 1, False, ("", False, "")),
            ("model text about limits is not a limit", events(text("m1", LIMIT)), "", 0, False, (LIMIT, False, "")),
            ("stderr about limits without an error event is not a limit", events(text("m1", "ok")), LIMIT, 0, False, ("ok", False, "")),
            ("timed out is never a limit", events(error(LIMIT)), "", -15, True, ("", False, "")),
        ]
        for name, stdout, stderr, code, timed_out, (want_text, limit, reset) in table:
            with self.subTest(name):
                result = parse_session(stdout, stderr, code, timed_out)
                self.assertEqual((result.text, result.usage_limit, result.usage_reset), (want_text, limit, reset))
                self.assertEqual((result.returncode, result.timed_out), (code, timed_out))

    def test_reset_is_capped_and_single_line(self) -> None:
        result = parse_session(events(error("Rate limit exceeded. resets " + "a\n" * 300)), "", 1)
        self.assertEqual(len(result.usage_reset), 200)
        self.assertNotIn("\n", result.usage_reset)


if __name__ == "__main__":
    unittest.main()
