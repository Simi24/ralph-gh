"""Table test for the claude adapter's output parsing and usage-limit detection (critical path)."""
import json
import unittest

from conductor.claude_output import parse_session
from conductor.usage_limit import UNKNOWN_RESET


def out(**fields: object) -> str:
    return json.dumps({"type": "result", "subtype": "success", **fields})


LIMIT = "You've hit your session limit. Resets at 3pm (Europe/Rome)."
RESET = "Resets at 3pm (Europe/Rome)"
LONG_GAP = "You've hit your " + "x" * 41 + " limit"


class ParseSessionTest(unittest.TestCase):
    def test_text_and_flags(self) -> None:
        # (name, stdout, stderr, returncode, timed_out, (text, usage_limit, reset))
        table = [
            ("plain result", out(is_error=False, result="done\nGATE:PASS"), "", 0, False, ("done\nGATE:PASS", False, "")),
            ("no JSON", "oops", "boom", 1, False, ("", False, "")),
            ("empty stdout", "", "", 0, False, ("", False, "")),
            ("result is not a string", out(result=None), "", 0, False, ("", False, "")),
            ("event list: last result wins", json.dumps([{"type": "system"}, {"type": "result", "result": "ok"}]), "", 0, False, ("ok", False, "")),
            ("limit wording, is_error true", out(is_error=True, result=LIMIT), "", 1, False, (LIMIT, True, RESET)),
            ("limit wording but is_error false", out(is_error=False, result=LIMIT), "", 0, False, (LIMIT, False, "")),
            ("is_error missing", out(result=LIMIT), "", 0, False, (LIMIT, False, "")),
            ("is_error is the string true", out(is_error="true", result=LIMIT), "", 0, False, (LIMIT, False, "")),
            ("limit only in stderr", out(is_error=True, result="x"), LIMIT, 1, False, ("x", True, RESET)),
            ("limit in errors[]", out(is_error=True, errors=[LIMIT], result=""), "", 1, False, ("", True, RESET)),
            ("bare limit", out(is_error=True, result="You've hit your limit"), "", 1, False, ("You've hit your limit", True, UNKNOWN_RESET)),
            ("credits", out(is_error=True, result="You're out of usage credits"), "", 1, False, ("You're out of usage credits", True, UNKNOWN_RESET)),
            ("org", out(is_error=True, result="Your org is out of usage. continuing automatically at noon"), "", 1, False, ("Your org is out of usage. continuing automatically at noon", True, "continuing automatically at noon")),
            ("other error is not a limit", out(is_error=True, result="API error 500"), "", 1, False, ("API error 500", False, "")),
            ("gap longer than 40 chars", out(is_error=True, result=LONG_GAP), "", 1, False, (LONG_GAP, False, "")),
            ("timed out is never a limit", out(is_error=True, result=LIMIT), "", -15, True, ("", False, "")),
        ]
        for name, stdout, stderr, code, timed_out, (text, limit, reset) in table:
            with self.subTest(name):
                result = parse_session(stdout, stderr, code, timed_out)
                self.assertEqual((result.text, result.usage_limit, result.usage_reset), (text, limit, reset))
                self.assertEqual((result.returncode, result.timed_out), (code, timed_out))

    def test_reset_is_capped_and_single_line(self) -> None:
        result = parse_session(out(is_error=True, result="You've hit your limit. resets " + "a\n" * 300), "", 1)
        self.assertEqual(len(result.usage_reset), 200)
        self.assertNotIn("\n", result.usage_reset)


if __name__ == "__main__":
    unittest.main()
