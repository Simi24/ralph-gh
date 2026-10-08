"""One ticket from queued to integrated, with fix rounds.

A failing verify or a ticket-gate FAIL starts a fix session (bounded by
`gate_fix_rounds`, shared by both kinds) and the ticket is re-verified and
re-gated. Exhausted rounds mean `ralph:failed:issue` with a diagnosis comment
and the PR left open. A session reporting "blocked" is an escalation
(`ralph:blocked`), never a failure. A gate verdict that cannot be parsed is
retried once with a fresh session and is never a PASS.

A ticket flow is a generator, so many run at once on one conductor thread
(conductor/scheduler.py). It yields a `Job` for every step that only runs a
session or a local command; the scheduler runs jobs in worker threads and
sends each result back. Everything else (labels, PRs, merges, comments, git)
happens in the generator body, so only the conductor thread writes.
A PR that stops merging cleanly goes to a merge-fix session (bounded by its
own counter, also `gate_fix_rounds`), then is re-verified and gated again: a bad
resolution could drop code of an integrated ticket. The new gate is told which
commit resolved the conflict; a FAIL goes through the normal fix rounds.

A `git` or `gh` failure inside a flow (an `InfraError`) is an infrastructure
failure, never a traceback: the ticket gets `ralph:failed:systemic` and a
diagnosis comment, its worktree goes, and independent tickets carry on. So does
a merge refused for a reason other than a conflict. A refused merge whose PR
turns out to be merged already is an integrated ticket.
"""
import logging
from collections.abc import Callable, Generator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from conductor import final, labels
from conductor.config import Config
from conductor.findings import blocking_findings, tail
from conductor.markers import OutcomeKind, Verdict, outcome_of, usable, verdict_of
from conductor.naming import ticket_branch
from conductor.observer import NullObserver, Observer
from conductor.ports import Agents, Forge, Git, InfraError, Issue, PullRequest, SessionRequest
from conductor.prompts import fix_prompt, implementer_prompt, merge_fix_prompt, ticket_gate_prompt
from conductor.sessions import SessionRegistry
from conductor.state_dir import prd_dir
from conductor.verify import check_verify

log = logging.getLogger("conductor")


class TicketStatus(Enum):
    INTEGRATED = "integrated"
    FAILED = "failed"
    BLOCKED = "blocked"
    SYSTEMIC = "systemic"  # a git/gh failure, not the ticket's fault


@dataclass(frozen=True)
class TicketResult:
    status: TicketStatus
    reason: str


def _writer(config: Config, role: str, prompt: str, cwd: Path, notes: Path | None) -> SessionRequest:
    return SessionRequest(
        role=role,
        prompt=prompt,
        cwd=cwd,
        model=config.model,
        timeout=config.session_timeout,
        add_dirs=(notes.parent,) if notes else (),
    )


def _gate(
    config: Config, agents: Agents, ticket: int, integration: str, cwd: Path, merge_commit: str | None = None
) -> tuple[Verdict, str, SessionResult]:
    """One gate, retried once with a fresh session when a healthy session gave
    no parsable verdict. Timeouts and usage limits are not retried."""
    request = SessionRequest(
        role="ticket-gate",
        prompt=ticket_gate_prompt(config, ticket, integration, merge_commit),
        cwd=cwd,
        agent=config.ticket_gate_agent,
        timeout=config.session_timeout,
    )
    for attempt in (1, 2):
        result = agents.start(request)
        verdict = verdict_of(result)
        if verdict is not Verdict.UNPARSABLE or not usable(result):
            return verdict, result.text, result
        log.warning("ticket #%s: gate attempt %s gave no parsable verdict", ticket, attempt)
    return Verdict.UNPARSABLE, "", result


_STOP_LABEL = {
    TicketStatus.BLOCKED: labels.BLOCKED,
    TicketStatus.FAILED: labels.FAILED_ISSUE,
    TicketStatus.SYSTEMIC: labels.FAILED_SYSTEMIC,
}
_STOP_KIND = {
    TicketStatus.BLOCKED: "blocked, needs a human",
    TicketStatus.FAILED: "failed",
    TicketStatus.SYSTEMIC: "failed (infrastructure: git or gh, not the ticket)",
}


def _stop(
    forge: Forge, git: Git, worktree: Path, branch: str, ticket: Issue, status: TicketStatus, reason: str,
    push: bool = True,
) -> TicketResult:
    """Leave the ticket for a human: keep the work on the remote, label, comment."""
    if push:
        try:
            git.push(worktree, branch)  # so the work (and any open PR) survives the worktree
        except Exception as error:
            log.warning("ticket #%s: could not push %s: %s", ticket.number, branch, error)
    forge.set_labels(ticket.number, add=(_STOP_LABEL[status],), remove=(labels.IN_PROGRESS, labels.IN_REVIEW))
    kind = _STOP_KIND[status]
    forge.comment(ticket.number, f"ralph-gh: ticket #{ticket.number} {kind}. {reason}")
    return TicketResult(status, f"ticket #{ticket.number} {kind}: {reason}")


Job = Callable[[], Any]
Flow = Generator[Job, Any, TicketResult]


def _session(agents: Agents, request: SessionRequest) -> Job:
    return lambda: agents.start(request)


def integrate_ticket(
    config: Config, forge: Forge, agents: Agents, git: Git, ticket: Issue, integration: str, notes: Path | None,
    obs: Observer | None = None, sessions: SessionRegistry | None = None, resume: PullRequest | None = None,
) -> Flow:
    """`resume`: the open PR of a ticket an earlier run left in review. It is not claimed or
    re-implemented: its branch is checked out and goes through verify, gate and merge."""
    obs = obs or NullObserver()
    branch = ticket_branch(config.branch_prefix, config.prd, ticket.number)
    worktree = prd_dir(config.state_root, config.prd) / "worktrees" / f"ticket-{ticket.number}"

    try:
        if resume is None:
            forge.set_labels(ticket.number, add=(labels.IN_PROGRESS,), remove=(labels.QUEUED,))
            _discard_closed_branch(forge, git, branch, integration)
        git.add_worktree(worktree, branch, integration if resume is None else branch)
    except InfraError as error:
        return _stop(forge, git, worktree, branch, ticket, TicketStatus.SYSTEMIC, f"setup failed: {error}", push=False)
    try:
        return (yield from _flow(
            config, forge, agents, git, ticket, integration, notes, obs, branch, worktree, sessions, resume
        ))
    except InfraError as error:  # a git/gh call failed: this ticket ends, the others carry on
        return _stop(forge, git, worktree, branch, ticket, TicketStatus.SYSTEMIC, str(error))
    finally:
        try:
            git.remove_worktree(worktree, branch)  # integrated, failed or blocked
        except InfraError as error:
            log.warning("ticket #%s: could not remove its worktree: %s", ticket.number, error)


def _discard_closed_branch(forge: Forge, git: Git, branch: str, integration: str) -> None:
    """A human closed this ticket's last PR unmerged and the ticket is redone: a push from a
    fresh worktree would be rejected as non-fast-forward against the stale remote branch.
    Delete that branch first (GitHub keeps the closed PR's commits under refs/pull/N/head,
    so no work is lost). Never a force-push. Raises on any doubt: the caller fails the ticket."""
    previous = forge.latest_pr(head=branch, base=integration)
    if previous is not None and previous.state == "closed":
        git.delete_remote_branch(branch)


def _merged_anyway(forge: Forge, pr: PullRequest, branch: str, integration: str) -> bool:
    """A merge reported as refused may have gone through: re-read the PR before deciding."""
    latest = forge.latest_pr(head=branch, base=integration)
    return latest is not None and latest.number == pr.number and latest.state == "merged"


def _flow(
    config: Config, forge: Forge, agents: Agents, git: Git, ticket: Issue, integration: str, notes: Path | None,
    obs: Observer, branch: str, worktree: Path, sessions: SessionRegistry | None, resume: PullRequest | None,
) -> Flow:
    n = ticket.number

    def stop(status: TicketStatus, reason: str) -> TicketResult:
        return _stop(forge, git, worktree, branch, ticket, status, reason)

    if resume is None:
        session = yield _session(
            agents, _writer(config, "implementer", implementer_prompt(config, n, integration, notes), worktree, notes)
        )
        outcome = outcome_of(session)
        if outcome.kind is OutcomeKind.BLOCKED:
            return stop(TicketStatus.BLOCKED, outcome.reason or "no reason given")
        if outcome.kind is not OutcomeKind.DONE:
            return stop(TicketStatus.FAILED, "the implementer gave no usable outcome")

    pr: PullRequest | None = resume
    rounds = 0  # verify-fix and gate-fix rounds together
    merge_rounds = 0  # merge-fix attempts, bounded apart from the rounds above
    gated = False  # the current code has a PASS
    merge_commit: str | None = None  # the last merge-fix's commit: the gate is told to inspect it
    while True:
        with obs.phase(n, "verify"):  # the first run and every re-run after a fix
            verify = yield lambda: check_verify(config.verify_commands, worktree, sessions)
        if verify.ok:
            git.push(worktree, branch)
            head_sha = git.head_sha(worktree)
            if pr is None:
                pr = forge.create_pr(
                    head=branch,
                    base=integration,
                    title=f"{ticket.title} (#{n})",
                    body=f"Ticket #{n} of PRD #{config.prd}.",
                )
                forge.set_labels(n, add=(labels.IN_REVIEW,), remove=(labels.IN_PROGRESS,))
            text = ""
            if not gated:
                verdict, text, _ = yield lambda: _gate(config, agents, n, integration, worktree, merge_commit)
                if verdict is Verdict.UNPARSABLE:
                    return stop(
                        TicketStatus.FAILED,
                        "the gate gave no parsable verdict, even after a retry: the ticket was never judged.",
                    )
                gated = verdict is Verdict.PASS
            if gated:
                if forge.merge_pr(pr.number, method="merge", head_sha=head_sha) or _merged_anyway(
                    forge, pr, branch, integration
                ):
                    forge.set_labels(n, add=(labels.INTEGRATED,), remove=(labels.IN_REVIEW,))
                    final.open_draft_after_first_merge(config, forge, integration)
                    return TicketResult(TicketStatus.INTEGRATED, f"ticket #{n} integrated")
                # Refused. Only a moved integration branch (a conflict) is the ticket's
                # to repair; any other refusal is a failure of the merge itself.
                if not _behind(git, worktree, integration):
                    return stop(
                        TicketStatus.SYSTEMIC, f"the merge of PR #{pr.number} was refused for a reason other than a conflict"
                    )
                if merge_rounds >= config.gate_fix_rounds:
                    return stop(
                        TicketStatus.FAILED,
                        f"PR #{pr.number} still does not merge after {merge_rounds} merge-fix attempt(s).",
                    )
                merge_rounds += 1
                request = _writer(config, "merge-fix", merge_fix_prompt(config, n, integration, notes), worktree, notes)
                fixed = outcome_of((yield _session(agents, request)))
                if fixed.kind is OutcomeKind.BLOCKED:
                    return stop(TicketStatus.BLOCKED, fixed.reason or "no reason given")
                if fixed.kind is not OutcomeKind.DONE:
                    return stop(TicketStatus.FAILED, f"merge-fix attempt {merge_rounds} gave no usable outcome")
                merge_commit = git.head_sha(worktree)  # the resolution: re-verified, then gated again
                gated = False
                continue
            findings, from_section = blocking_findings(text)
            fix = fix_prompt(config, n, integration, notes, reason="gate", context=findings, from_section=from_section)
            failure = f"the ticket gate failed after {rounds} fix round(s)."
        else:
            fix = fix_prompt(config, n, integration, notes, reason="verify", context=tail(verify.output))
            failure = f"verify still failing after {rounds} fix round(s).\n\n```\n{tail(verify.output, 40)}\n```"

        if rounds >= config.gate_fix_rounds:
            return stop(TicketStatus.FAILED, failure)
        rounds += 1
        gated = False  # a fix changes what the gate judged
        fixed = outcome_of((yield _session(agents, _writer(config, "fix", fix, worktree, notes))))
        if fixed.kind is OutcomeKind.BLOCKED:
            return stop(TicketStatus.BLOCKED, fixed.reason or "no reason given")
        if fixed.kind is not OutcomeKind.DONE:
            return stop(TicketStatus.FAILED, f"fix round {rounds} gave no usable outcome")


def _behind(git: Git, worktree: Path, integration: str) -> bool:
    """True if the integration branch has commits the ticket branch lacks. Errors read as "no"."""
    try:
        return git.is_behind(worktree, integration)
    except Exception as error:
        log.warning("could not tell whether %s moved: %s", integration, error)
        return False
