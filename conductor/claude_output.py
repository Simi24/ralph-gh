"""Turn a `claude --print --output-format json` run into a `SessionResult` (critical path).

Parsing: the session's text is the JSON object's `result` string; anything
else (no JSON, no string) is empty text, which every caller reads as
"unparsable", never as a verdict.

Usage-limit detection (exploration 4.3), in this order:
1. a timed-out session is never a usage limit (a real failure);
2. `is_error` must be exactly `true` (set by the CLI, never by model text, so
   a reply that merely discusses limits cannot match);
3. only then is the wording searched: `errors[]`, then `result`, then stderr.
The reset text is for log and exit-reason output only and is never parsed.
"""
import json
import logging
import re
from typing import Any

from conductor.ports import SessionResult
from conductor.usage_limit import UNKNOWN_RESET

log = logging.getLogger("conductor")

_LIMIT = re.compile(r"(You've hit your[^.\"]{0,40} limit|You're out of usage credits|Your org is out of usage)")
_RESET = re.compile(r"(resets?|continuing automatically at)[^.\"]*", re.IGNORECASE)
_RESET_MAX = 200


def _payload(stdout: str) -> dict[str, Any]:
    """The result object of the CLI's JSON output ({} when there is none)."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {}
    if isinstance(data, list):  # some CLI versions emit the event list: take the last result event
        data = next((e for e in reversed(data) if isinstance(e, dict) and e.get("type") == "result"), {})
    return data if isinstance(data, dict) else {}


def _text(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    return result if isinstance(result, str) else ""


def usage_limit_reset(payload: dict[str, Any], stderr: str) -> str | None:
    """None when this is not a usage limit; else the reset description (never empty)."""
    if payload.get("is_error") is not True:
        return None
    errors = payload.get("errors")
    parts = [str(e) for e in errors] if isinstance(errors, list) else []
    corpus = "\n".join([*parts, _text(payload), stderr])
    if not _LIMIT.search(corpus):
        return None
    found = _RESET.search(corpus)
    description = found.group(0).replace("\n", " ")[:_RESET_MAX].strip() if found else ""
    return description or UNKNOWN_RESET


def parse_session(stdout: str, stderr: str, returncode: int, timed_out: bool = False) -> SessionResult:
    if timed_out:
        return SessionResult(returncode=returncode, timed_out=True)
    payload = _payload(stdout)
    reset = usage_limit_reset(payload, stderr)
    if reset is not None:
        return SessionResult(text=_text(payload), returncode=returncode, usage_limit=True, usage_reset=reset)
    text = _text(payload)
    if not text:
        log.warning(f"claude session produced no parsable JSON result (exit {returncode}): {stderr.strip()[:300]}")
    return SessionResult(text=text, returncode=returncode)
