"""The integration branch is verified after every merge (critical path: merge decisions).

Two tickets can merge without a textual conflict and still break the build
together, and a ticket verified alone proves nothing about that. So after each
ticket merge the conductor verifies THAT merge commit (a pinned sha, in a
throwaway detached worktree) before anything else happens:

- `IntegrationGuard` is the state the scheduler reads: the last green sha, whether a check is
  due, whether the branch is broken. A ticket is dispatched, and a ticket PR merged, only while
  it is settled (green, no check due or running). Sessions already running carry on.
- `check_integration` is a flow like a ticket flow (a generator yielding `Job`s, the conductor
  thread does every git and forge write). Red starts an integration-fix session in its own
  worktree on the integration branch; the conductor re-verifies and pushes `HEAD:<integration>`
  (never forced), bounded by `gate_fix_rounds`.
- Exhausted rounds, a BLOCKED or unparsable fix session, or a failed push mean the integration
  branch is broken: `block_prd` labels the PRD `ralph:blocked` with a comment and the run drains
  (exit 3). No ticket is ever labelled failed for an integration break.

A usage limit or a stop during a check is the scheduler's business (park, drain), never a failure.
"""
import logging
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conductor import labels
from conductor.attempt import attempt
from conductor.config import Config
from conductor.findings import tail
from conductor.markers import OutcomeKind, outcome_of
from conductor.observer import Observer
from conductor.ports import Agents, Forge, Git, InfraError, SessionRequest
from conductor.prompts import integration_fix_prompt
from conductor.sessions import SessionRegistry
from conductor.state_dir import prd_dir
from conductor.verify import check_verify

log = logging.getLogger("conductor")

WAIT_FOR_GREEN = object()  # a ticket flow yields this before a merge: the scheduler resumes it once the branch is settled


@dataclass
class IntegrationGuard:
    """What the scheduler needs to know about the integration branch. Conductor thread only."""

    green_sha: str | None = None  # the last commit verified green
    needs_check: bool = False  # the tip is unverified (a merge, a resumed run, a parked check)
    broken: bool = False  # a check failed for good: nothing is dispatched or merged any more
    merged: list[int] = field(default_factory=list)  # tickets merged since `green_sha`

    @property
    def settled(self) -> bool:
        """The tip is the last green commit and nothing is due: safe to dispatch and to merge."""
        return not self.needs_check and not self.broken

    def note_merge(self, ticket: int) -> None:
        self.needs_check = True
        self.merged.append(ticket)

    def note_green(self, sha: str) -> None:
        self.green_sha = sha
        self.needs_check = False
        self.merged.clear()


def initial_guard(config: Config, git: Git, integration: str) -> IntegrationGuard:
    """A fresh branch (nothing beyond the base) is the baseline. Anything else, such as a resumed
    run with merges on the branch, must be verified before the first dispatch. Fail closed."""
    try:
        tip = git.remote_sha(integration)
        base = git.remote_sha(config.base_branch)
        if git.is_ancestor(tip, base):
            return IntegrationGuard(green_sha=tip)
    except Exception as error:  # noqa: BLE001 - unknown means "verify it"
        log.warning("could not compare %s with the base branch: %s", integration, error)
    return IntegrationGuard(needs_check=True)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    sha: str  # green: the verified (possibly fixed) tip; red: the commit that failed
    detail: str = ""  # red: why, with the verify tail


def _tickets(numbers: list[int]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def broken_reason(merged: list[int]) -> str:
    return f"integration branch broken after the merge of {_tickets(merged)}" if merged else "integration branch broken"


def block_prd(config: Config, forge: Forge, result: CheckResult, merged: list[int]) -> str:
    """Escalate to a human: PRD `ralph:blocked` and a comment. Returns the one-line reason."""
    reason = broken_reason(merged)
    attempt(lambda: forge.set_labels(config.prd, add=(labels.BLOCKED,)))
    attempt(
        lambda: forge.comment(
            config.prd,
            f"ralph-gh: {reason} (`{result.sha}`). The run is draining; the merged tickets stay "
            f"{labels.INTEGRATED}.\n\n{result.detail}",
        )
    )
    return reason


def _session_job(agents: Agents, request: SessionRequest) -> Any:
    return lambda: agents.start(request)


def check_integration(
    config: Config, agents: Agents, git: Git, integration: str, notes: Path | None, sessions: SessionRegistry | None,
    obs: Observer, sha: str, green_sha: str | None, merged: list[int],
) -> Generator[Any, Any, CheckResult]:
    """Verify `sha` (the integration tip, pinned); when red, repair it with integration-fix rounds."""
    try:
        return (yield from _check(config, agents, git, integration, notes, sessions, obs, sha, green_sha, merged))
    except InfraError as error:  # fail closed: a branch that cannot be checked is not green
        return CheckResult(False, sha, f"could not check the integration branch: {error}")


def _remove(remove: Any, what: str) -> None:
    try:
        remove()
    except InfraError as error:
        log.warning("could not remove the %s worktree: %s", what, error)


def _check(
    config: Config, agents: Agents, git: Git, integration: str, notes: Path | None, sessions: SessionRegistry | None,
    obs: Observer, sha: str, green_sha: str | None, merged: list[int],
) -> Generator[Any, Any, CheckResult]:
    trees = prd_dir(config.state_root, config.prd) / "worktrees"
    verify_tree = trees / "integration-verify"
    git.add_detached_worktree(verify_tree, sha)
    try:
        obs.event(None, f"integration verify started on {sha[:7]}")
        verify = yield lambda: check_verify(config.verify_commands, verify_tree, sessions)
    finally:
        _remove(lambda: git.remove_detached_worktree(verify_tree), "integration-verify")
    if verify.ok:
        obs.event(None, f"integration verify green on {sha[:7]}")
        return CheckResult(True, sha)
    obs.event(None, f"integration verify RED on {sha[:7]}")

    shown = tail(verify.output, 40)
    fix_tree = trees / "integration-fix"
    branch = f"{integration}-integration-fix"
    git.add_worktree(fix_tree, branch, integration)
    try:
        for round_no in range(1, config.gate_fix_rounds + 1):
            request = SessionRequest(
                role="integration-fix",
                prompt=integration_fix_prompt(
                    config, integration, notes, red_sha=sha, green_sha=green_sha, tickets=merged, verify_tail=shown
                ),
                cwd=fix_tree,
                model=config.model,
                timeout=config.session_timeout,
                add_dirs=(notes.parent,) if notes else (),
            )
            fixed = outcome_of((yield _session_job(agents, request)))
            if fixed.kind is OutcomeKind.BLOCKED:
                reason = fixed.reason or "no reason given"
                return CheckResult(False, sha, f"The integration-fix session reported blocked: {reason}\n\n```\n{shown}\n```")
            if fixed.kind is not OutcomeKind.DONE:
                return CheckResult(False, sha, f"The integration-fix session (round {round_no}) gave no usable outcome.\n\n```\n{shown}\n```")
            verify = yield lambda: check_verify(config.verify_commands, fix_tree, sessions)
            if verify.ok:
                try:
                    git.push(fix_tree, integration)  # fast-forward only, never forced
                except InfraError as error:
                    return CheckResult(False, sha, f"Could not push the fix to {integration}: {error}")
                fixed_sha = git.head_sha(fix_tree)
                obs.event(None, f"integration branch repaired: {fixed_sha[:7]}")
                return CheckResult(True, fixed_sha)
            shown = tail(verify.output, 40)
        return CheckResult(
            False, sha, f"Verify still fails after {config.gate_fix_rounds} integration-fix round(s).\n\n```\n{shown}\n```"
        )
    finally:
        _remove(lambda: git.remove_worktree(fix_tree, branch), "integration-fix")
