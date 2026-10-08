"""One fix round on the integration branch after a failed final review.

A throwaway worktree on `<integration>-final-fix` is based on the integration
branch; the session fixes the BLOCKING findings, the conductor re-runs verify
and only then pushes to the integration branch (fast-forward, never forced).
"""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from conductor.config import Config
from conductor.final_prompt import final_fix_prompt
from conductor.findings import blocking_findings, tail
from conductor.markers import OutcomeKind, outcome_of
from conductor.observer import NullObserver, Observer
from conductor.ports import Agents, Git, SessionRequest
from conductor.usage_limit import describe
from conductor.verify import check_verify


class FixStatus(Enum):
    FIXED = "fixed"
    BLOCKED = "blocked"
    FAILED = "failed"
    USAGE_LIMIT = "usage limit"


@dataclass(frozen=True)
class FixRound:
    status: FixStatus
    detail: str = ""


def run_fix_round(
    config: Config, agents: Agents, git: Git, integration: str, notes: Path | None, verdict: str, round: int,
    obs: Observer | None = None,
) -> FixRound:
    obs = obs or NullObserver()
    findings, from_section = blocking_findings(verdict)
    worktree = config.state_root / f"prd-{config.prd}" / "worktrees" / "final-fix"
    branch = f"{integration}-final-fix"
    git.add_worktree(worktree, branch, integration)
    try:
        result = agents.start(
            SessionRequest(
                role="final-fix",
                prompt=final_fix_prompt(
                    config, integration, notes, round=round, findings=findings, from_section=from_section
                ),
                cwd=worktree,
                model=config.model,
                timeout=config.session_timeout,
                add_dirs=(notes.parent,) if notes else (),
            )
        )
        if result.usage_limit:
            return FixRound(FixStatus.USAGE_LIMIT, describe(result))
        outcome = outcome_of(result)
        if outcome.kind is OutcomeKind.BLOCKED:
            return FixRound(FixStatus.BLOCKED, outcome.reason or "no reason given")
        if outcome.kind is not OutcomeKind.DONE:
            return FixRound(FixStatus.FAILED, "the fix session gave no usable outcome")
        with obs.phase(config.prd, "verify"):
            verify = check_verify(config.verify_commands, worktree)
        if not verify.ok:
            return FixRound(FixStatus.FAILED, f"verify fails after the fix:\n\n```\n{tail(verify.output, 40)}\n```")
        try:
            git.push(worktree, integration)
        except Exception as error:  # noqa: BLE001 - e.g. the branch moved: nothing was pushed
            return FixRound(FixStatus.FAILED, f"could not push the fix to {integration}: {error}")
        return FixRound(FixStatus.FIXED)
    finally:
        git.remove_worktree(worktree, branch)
