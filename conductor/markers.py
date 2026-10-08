"""Pure marker parser for session output (critical path: authorizes merges).

Rules (DECISIONS 1): whole-line matches only, surrounded by whitespace and 0-2
backticks; only the last 5 non-blank lines count; the last marker of a family
wins; two different markers of one family in the window are unparsable; a
killed, timed-out or failed session is never usable.
"""
import re
from dataclasses import dataclass
from enum import Enum

from conductor.ports import SessionResult

WINDOW = 5

_VERDICT = re.compile(r"^\s*`{0,2}GATE:(PASS|FAIL)`{0,2}\s*$")
_OUTCOME = re.compile(r"^\s*`{0,2}RALPH:(DONE|BLOCKED)(?:[ \t]+(\S.*?))?`{0,2}\s*$")


class Verdict(Enum):
    PASS = "pass"
    FAIL = "fail"
    UNPARSABLE = "unparsable"


class OutcomeKind(Enum):
    DONE = "done"
    BLOCKED = "blocked"
    UNPARSABLE = "unparsable"


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    reason: str = ""


def _window(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()][-WINDOW:]


def parse_verdict(text: str) -> Verdict:
    found = {m.group(1) for line in _window(text) if (m := _VERDICT.match(line))}
    if found == {"PASS"}:
        return Verdict.PASS
    if found == {"FAIL"}:
        return Verdict.FAIL
    return Verdict.UNPARSABLE  # nothing found, or PASS and FAIL together


def parse_outcome(text: str) -> Outcome:
    found = [m for line in _window(text) if (m := _OUTCOME.match(line))]
    kinds = {m.group(1) for m in found}
    if kinds == {"DONE"}:
        return Outcome(OutcomeKind.DONE)
    if kinds == {"BLOCKED"}:
        return Outcome(OutcomeKind.BLOCKED, found[-1].group(2) or "")
    return Outcome(OutcomeKind.UNPARSABLE)  # nothing found, or DONE and BLOCKED together


def usable(result: SessionResult) -> bool:
    return result.returncode == 0 and not result.timed_out and not result.usage_limit


def verdict_of(result: SessionResult) -> Verdict:
    """Session status is checked before its text."""
    return parse_verdict(result.text) if usable(result) else Verdict.UNPARSABLE


def outcome_of(result: SessionResult) -> Outcome:
    return parse_outcome(result.text) if usable(result) else Outcome(OutcomeKind.UNPARSABLE)
