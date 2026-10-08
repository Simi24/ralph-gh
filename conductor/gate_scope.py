"""How much of the final PR a re-review covers (critical path: decides what a
merge-authorizing review looked at).

A re-review after a fix round is scoped to the fix diff only when every input
can be trusted; otherwise the whole PR is reviewed again. Checked in order:
a fix round happened, the previous verdict is a non-empty file, both shas are
40-hex, the head moved, and the reviewed commit is an ancestor of the head.
"""
import re
from enum import Enum
from pathlib import Path

from conductor.ports import Git

_SHA = re.compile(r"^[0-9a-f]{40}$")


class Scope(Enum):
    FULL = "full"
    FIX_DIFF = "fix-diff"


def resolve_gate_scope(git: Git, *, round: int, prev_sha: str, head_sha: str, prev_verdict: Path | None) -> Scope:
    if round <= 1 or prev_verdict is None:
        return Scope.FULL
    try:
        if not prev_verdict.is_file() or prev_verdict.stat().st_size == 0:
            return Scope.FULL
    except OSError:
        return Scope.FULL
    if not (_SHA.match(prev_sha) and _SHA.match(head_sha)):
        return Scope.FULL
    if prev_sha == head_sha:  # the fix never reached the PR: an empty diff proves nothing
        return Scope.FULL
    try:
        if not git.is_ancestor(prev_sha, head_sha):
            return Scope.FULL
    except Exception:  # noqa: BLE001 - fail closed: doubt means a full review
        return Scope.FULL
    return Scope.FIX_DIFF
