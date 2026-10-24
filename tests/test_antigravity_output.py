"""Table test for the Antigravity adapter's output parsing and usage-limit detection."""
import json
import unittest

from conductor.antigravity_output import parse_session
from conductor.usage_limit import UNKNOWN_RESET


def agy_out(**fields: object) -> str:
    defaults = {"status": "SUCCESS", "response": ""}
    return json.dumps({**defaults, **fields})


LIMIT = "You've hit your limit. Resets at 3pm."
RESET = "Resets at 3pm"


class AntigravityParseSessionTest(unittest.TestCase):
    def test_text_and_flags(self) -> None:
        table = [
            ("plain response", agy_out(response="done\nGATE:PASS"), "", 0, False, ("done\nGATE:PASS", False, "")),
            ("no JSON", "oops", "boom", 1, False, ("", False, "")),
            ("empty stdout", "", "", 0, False, ("", False, "")),
            ("response is not a string", agy_out(response=None), "", 0, False, ("", False, "")),
            ("result fallback", json.dumps({"result": "ok"}), "", 0, False, ("ok", False, "")),
            ("event list: last response wins", json.dumps([{"type": "system"}, {"status": "SUCCESS", "response": "ok"}]), "", 0, False, ("ok", False, "")),
            ("limit wording with status ERROR", agy_out(status="ERROR", error=LIMIT), "", 1, False, ("", True, RESET)),
            ("limit wording in response with status ERROR", agy_out(status="ERROR", response=LIMIT), "", 1, False, (LIMIT, True, RESET)),
            ("limit only in stderr with error status", agy_out(status="ERROR", response="fail"), LIMIT, 1, False, ("fail", True, RESET)),
            ("quota exceeded", agy_out(status="ERROR", error="Quota exceeded for model"), "", 1, False, ("", True, UNKNOWN_RESET)),
            ("resource exhausted", agy_out(status="ERROR", error="RESOURCE_EXHAUSTED"), "", 1, False, ("", True, UNKNOWN_RESET)),
            ("rate limit exceeded", agy_out(status="ERROR", error="Rate limit exceeded"), "", 1, False, ("", True, UNKNOWN_RESET)),
            ("other error is not a limit", agy_out(status="ERROR", error="Compilation failed"), "", 1, False, ("", False, "")),
            ("timed out is never a limit", agy_out(status="ERROR", error=LIMIT), "", -15, True, ("", False, "")),
        ]
        for name, stdout, stderr, code, timed_out, (text, limit, reset) in table:
            with self.subTest(name):
                result = parse_session(stdout, stderr, code, timed_out)
                self.assertEqual((result.text, result.usage_limit, result.usage_reset), (text, limit, reset))
                self.assertEqual((result.returncode, result.timed_out), (code, timed_out))

    def test_reset_is_capped_and_single_line(self) -> None:
        result = parse_session(agy_out(status="ERROR", error="You've hit your limit. resets " + "a\n" * 300), "", 1)
        self.assertEqual(len(result.usage_reset), 200)
        self.assertNotIn("\n", result.usage_reset)


if __name__ == "__main__":
    unittest.main()
