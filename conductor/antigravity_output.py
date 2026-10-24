"""Turn an `agy --dangerously-skip-permissions --output-format json` run into a `SessionResult`.

Parsing: the session's text is the JSON object's `response` string (or `result` fallback);
anything else is empty text.

Usage-limit detection:
1. a timed-out session is never a usage limit (a real failure);
2. error status or error text matching known quota/rate limit patterns;
3. reset text is parsed if present, falling back to UNKNOWN_RESET.
"""
import json
import logging
import re
from typing import Any

from conductor.ports import SessionResult
from conductor.usage_limit import UNKNOWN_RESET

log = logging.getLogger("conductor")

_LIMIT = re.compile(
    r"(You've hit your[^.\"]{0,40} limit|You're out of usage credits|Your org is out of usage|"
    r"Quota exceeded|Rate limit exceeded|RESOURCE_EXHAUSTED|Too Many Requests)",
    re.IGNORECASE,
)
_RESET = re.compile(r"(resets?|continuing automatically at)[^.\"]*", re.IGNORECASE)
_RESET_MAX = 200


def _payload(stdout: str) -> dict[str, Any]:
    """The result object of the CLI's JSON output ({} when there is none)."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {}
    if isinstance(data, list):
        data = next((e for e in reversed(data) if isinstance(e, dict) and (e.get("status") or e.get("response"))), {})
    return data if isinstance(data, dict) else {}


def _text(payload: dict[str, Any]) -> str:
    resp = payload.get("response")
    if isinstance(resp, str):
        return resp
    res = payload.get("result")
    return res if isinstance(res, str) else ""


def usage_limit_reset(payload: dict[str, Any], stderr: str) -> str | None:
    """None when this is not a usage limit; else the reset description (never empty)."""
    is_err = payload.get("status") == "ERROR" or payload.get("is_error") is True
    err_msg = str(payload.get("error") or "")
    corpus = "\n".join([err_msg, _text(payload), stderr])
    if not is_err and not _LIMIT.search(corpus):
        return None
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
    if not text and returncode != 0:
        log.warning(f"antigravity session produced no parsable JSON response (exit {returncode}): {stderr.strip()[:300]}")
    return SessionResult(text=text, returncode=returncode)
