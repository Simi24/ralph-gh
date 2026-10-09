"""Autonomy decision for the final PR (critical path: authorizes a merge).

Fail closed: every unknown, unreadable or empty input withholds the merge.
"""
import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class MergeDecision:
    allowed: bool
    reason: str = ""


def _withheld(reason: str) -> MergeDecision:
    return MergeDecision(False, reason)


def decide_merge(
    autonomy: str,
    hitl_flagged: bool | None,
    allowlist: Sequence[str],
    changed_files: Sequence[str] | None,
) -> MergeDecision:
    """May the passing final PR be merged without a human?

    `hitl_flagged`: the PRD or any ticket carries ralph:hitl-arch (None = unknown).
    `changed_files`: the final diff's paths; only read for `yolo` (None = unreadable).
    """
    if autonomy == "halt-each-pr":
        return _withheld("autonomy=halt-each-pr")
    if autonomy not in ("respect-hitl-arch", "yolo"):
        return _withheld(f"unknown autonomy mode {autonomy!r}")
    if hitl_flagged is None:
        return _withheld("ralph:hitl-arch labels could not be read")
    if hitl_flagged:
        return _withheld("the PRD or a ticket is ralph:hitl-arch")
    if autonomy == "respect-hitl-arch":
        return MergeDecision(True)

    if not allowlist:
        return _withheld("yolo allowlist is empty")
    if not changed_files:
        return _withheld("yolo allowlist could not be verified (diff unavailable)")
    try:
        patterns = [re.compile(p) for p in allowlist]
    except re.error:
        return _withheld("yolo allowlist is not a valid regex")
    if any(not any(p.search(path) for p in patterns) for path in changed_files):
        return _withheld("diff outside yolo allowlist")
    return MergeDecision(True)
