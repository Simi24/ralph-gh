import unittest

from conductor.markers import OutcomeKind, Verdict, outcome_of, parse_outcome, parse_verdict, verdict_of
from conductor.ports import SessionResult

P, F, U = Verdict.PASS, Verdict.FAIL, Verdict.UNPARSABLE

VERDICT_CASES = [
    ("plain pass", "all good\nGATE:PASS", P),
    ("plain fail", "bad\nGATE:FAIL", F),
    ("trailing blank lines", "ok\nGATE:PASS\n\n\n\n", P),
    ("surrounding whitespace", "ok\n  GATE:PASS  ", P),
    ("one backtick", "ok\n`GATE:PASS`", P),
    ("two backticks", "ok\n``GATE:FAIL``", F),
    ("uneven backticks", "ok\n`GATE:PASS``", P),
    ("three backticks", "ok\n```GATE:PASS```", U),
    ("missing", "I reviewed the diff and it looks fine", U),
    ("empty", "", U),
    ("quoted in prose", "I will end with GATE:PASS when done", U),
    ("quoted contract inline", 'Reply "GATE:PASS" or "GATE:FAIL"', U),
    ("prefix text", "verdict: GATE:PASS", U),
    ("suffix text", "GATE:PASS because it works", U),
    ("truncated", "review done\nGATE:PA", U),
    ("lowercase", "gate:pass", U),
    ("last of same marker", "GATE:PASS\nmore text\nGATE:PASS", P),
    ("pass and fail is ambiguous", "GATE:FAIL\nnotes\nGATE:PASS", U),
    ("fail then pass reversed", "GATE:PASS\nnotes\nGATE:FAIL", U),
    ("contract quoted above window", "GATE:FAIL\na\nb\nc\nd\ne\nGATE:PASS", P),
    ("blank lines do not shrink window", "GATE:FAIL\n\na\n\nb\n\nc\n\nd\n\ne\nGATE:PASS", P),
    ("marker at edge of window", "GATE:PASS\na\nb\nc\nd", P),
    ("marker just outside window", "GATE:PASS\na\nb\nc\nd\ne", U),
]

OUTCOME_CASES = [
    ("done", "built it\nRALPH:DONE", OutcomeKind.DONE, ""),
    ("done backticks", "`RALPH:DONE`", OutcomeKind.DONE, ""),
    ("blocked with reason", "RALPH:BLOCKED need a human decision", OutcomeKind.BLOCKED, "need a human decision"),
    ("blocked backticks", "`RALPH:BLOCKED no access`", OutcomeKind.BLOCKED, "no access"),
    ("blocked last wins", "RALPH:BLOCKED first\nx\nRALPH:BLOCKED second", OutcomeKind.BLOCKED, "second"),
    ("done and blocked ambiguous", "RALPH:DONE\nRALPH:BLOCKED nope", OutcomeKind.UNPARSABLE, ""),
    ("missing", "finished the work", OutcomeKind.UNPARSABLE, ""),
    ("quoted in prose", "I will print RALPH:DONE at the end", OutcomeKind.UNPARSABLE, ""),
    ("truncated", "RALPH:DO", OutcomeKind.UNPARSABLE, ""),
]


class ParseVerdictTest(unittest.TestCase):
    def test_table(self) -> None:
        for name, text, expected in VERDICT_CASES:
            with self.subTest(name):
                self.assertEqual(parse_verdict(text), expected)


class ParseOutcomeTest(unittest.TestCase):
    def test_table(self) -> None:
        for name, text, kind, reason in OUTCOME_CASES:
            with self.subTest(name):
                outcome = parse_outcome(text)
                self.assertEqual((outcome.kind, outcome.reason), (kind, reason))


class SessionStatusTest(unittest.TestCase):
    def test_bad_session_status_is_never_usable(self) -> None:
        for name, result in [
            ("timed out", SessionResult("GATE:PASS", timed_out=True)),
            ("non-zero exit", SessionResult("GATE:PASS", returncode=1)),
            ("usage limit", SessionResult("GATE:PASS", usage_limit=True)),
        ]:
            with self.subTest(name):
                self.assertEqual(verdict_of(result), U)
                self.assertEqual(outcome_of(SessionResult("RALPH:DONE", result.returncode, result.timed_out, result.usage_limit)).kind, OutcomeKind.UNPARSABLE)

    def test_clean_session_is_parsed(self) -> None:
        self.assertEqual(verdict_of(SessionResult("GATE:PASS")), P)


if __name__ == "__main__":
    unittest.main()
