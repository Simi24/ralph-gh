"""Extract what a fix session may act on from a gate verdict.

The reviewer ends its verdict with a `### Blocking findings` section, then the
GATE marker line. Fix sessions get that section only, never FOLLOW-UP findings.
"""
import re

from conductor.markers import VERDICT_LINE

TAIL_LINES = 80
_HEADING = re.compile(r"^\s*#{1,6}\s*blocking findings\s*:?\s*$", re.IGNORECASE)


def blocking_findings(verdict: str) -> tuple[str, bool]:
    """(text, from_section). Falls back to the verdict's last lines when the
    section is missing or says `none`, with from_section False."""
    lines = verdict.splitlines()
    starts = [i for i, line in enumerate(lines) if _HEADING.match(line)]
    if starts:
        body = [line for line in lines[starts[-1] + 1:] if not VERDICT_LINE.match(line)]
        text = "\n".join(body).strip()
        if text and text.strip("`*_ .").lower() != "none":
            return text, True
    return "\n".join(lines[-TAIL_LINES:]).strip(), False


def tail(text: str, count: int = TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-count:])
