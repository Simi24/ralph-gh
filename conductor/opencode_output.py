"""Turn an `opencode run --format json` run into a `SessionResult` (critical path).

Parsing: stdout is one JSON event per line. The session's text is the text parts of the last
message that has any, joined by newlines (the outcome or verdict line lives there); lines that are
not JSON are skipped, and no text means empty text, which every caller reads as "unparsable".

Usage-limit detection, in this order:
1. a timed-out session is never a usage limit (a real failure);
2. there must be an `error` event (emitted by the CLI, never by model text, so a reply that merely
   discusses limits cannot match);
3. only then is the wording searched: the error events, then stderr. HTTP 429 counts as a limit.
The reset text is for log and exit-reason output only and is never parsed.
"""
import json
import logging
import re
from typing import Any

from conductor.ports import SessionResult
from conductor.usage_limit import UNKNOWN_RESET

log = logging.getLogger("conductor")

_LIMIT = re.compile(
    r"(rate limit|usage limit|limit reached|quota exceeded|Too Many Requests|RESOURCE_EXHAUSTED|"
    r"\"statusCode\": ?429|out of (?:usage )?credits|insufficient (?:balance|credits))",
    re.IGNORECASE,
)
_RESET = re.compile(r"(resets?|try again)[^.\"]*", re.IGNORECASE)
_RESET_MAX = 200


def _events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _text(events: list[dict[str, Any]]) -> str:
    """The text parts of the last message that has text."""
    last: str | None = None
    parts: list[str] = []
    for event in events:
        part = event.get("part")
        if event.get("type") != "text" or not isinstance(part, dict) or not isinstance(part.get("text"), str):
            continue
        message = str(part.get("messageID", ""))
        if message != last:
            last, parts = message, []
        parts.append(part["text"])
    return "\n".join(parts)


def usage_limit_reset(events: list[dict[str, Any]], stderr: str) -> str | None:
    """None when this is not a usage limit; else the reset description (never empty)."""
    errors = [json.dumps(e.get("error"), ensure_ascii=False) for e in events if e.get("type") == "error"]
    if not errors:
        return None
    corpus = "\n".join([*errors, stderr])
    if not _LIMIT.search(corpus):
        return None
    found = _RESET.search(corpus)
    description = found.group(0).replace("\\n", " ").replace("\n", " ")[:_RESET_MAX].strip() if found else ""
    return description or UNKNOWN_RESET


def parse_session(stdout: str, stderr: str, returncode: int, timed_out: bool = False) -> SessionResult:
    if timed_out:
        return SessionResult(returncode=returncode, timed_out=True)
    events = _events(stdout)
    text = _text(events)
    reset = usage_limit_reset(events, stderr)
    if reset is not None:
        return SessionResult(text=text, returncode=returncode, usage_limit=True, usage_reset=reset)
    if not text and returncode != 0:
        log.warning(f"opencode session produced no text (exit {returncode}): {stderr.strip()[:300]}")
    return SessionResult(text=text, returncode=returncode)
