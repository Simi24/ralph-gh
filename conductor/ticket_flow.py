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
own counter, also `gate_fix_rounds`) and is re-verified, not re-gated.
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
from conductor.markers import OutcomeKind, Verdict, outcome_of, verdict_of
from conductor.naming import ticket_branch
from conductor.observer import NullObserver, Observer
from conductor.ports import Agents, Forge, Git, Issue, PullRequest, SessionRequest, SessionResult
from conductor.prompts import fix_prompt, implementer_prompt, merge_fix_prompt, ticket_gate_prompt
from conductor.verify import check_verify

log = logging.getLogger("conductor")


class TicketStatus(Enum):
    INTEGRATED = "integrated"
    FAILED = "failed"
    BLOCKED = "blocked"


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


def _usable(result: SessionResult) -> bool:
    return result.returncode == 0 and not result.timed_out and not result.usage_limit


def _gate(
    config: Config, agents: Agents, ticket: int, integration: str, cwd: Path
) -> tuple[Verdict, str, SessionResult]:
    """One gate, retried once with a fresh session when a healthy session gave
    no parsable verdict. Timeouts and usage limits are not retried."""
    request = SessionRequest(
        role="ticket-gate",
        prompt=ticket_gate_prompt(config, ticket, integration),
        cwd=cwd,
        agent=config.ticket_gate_agent,
        timeout=config.session_timeout,
    )
    for attempt in (1, 2):
        result = agents.start(request)
        verdict = verdict_of(result)
        if verdict is not Verdict.UNPARSABLE or not _usable(result):
            return verdict, result.text, result
        log.warning("ticket #%s: gate attempt %s gave no parsable verdict", ticket, attempt)
    return Verdict.UNPARSABLE, "", result


def _stop(forge: Forge, git: Git, worktree: Path, branch: str, ticket: Issue, status: TicketStatus, reason: str) -> TicketResult:
    """Leave the ticket for a human: keep the work on the remote, label, comment."""
    try:
        git.push(worktree, branch)  # so the work (and any open PR) survives the worktree
    except Exception as error:
        log.warning("ticket #%s: could not push %s: %s", ticket.number, branch, error)
    label = labels.BLOCKED if status is TicketStatus.BLOCKED else labels.FAILED_ISSUE
    forge.set_labels(ticket.number, add=(label,), remove=(labels.IN_PROGRESS, labels.IN_REVIEW))
    kind = "blocked, needs a human" if status is TicketStatus.BLOCKED else "failed"
    forge.comment(ticket.number, f"ralph-gh: ticket #{ticket.number} {kind}. {reason}")
    return TicketResult(status, f"ticket #{ticket.number} {kind}: {reason}")


Job = Callable[[], Any]
Flow = Generator[Job, Any, TicketResult]


def _session(agents: Agents, request: SessionRequest) -> Job:
    return lambda: agents.start(request)


def integrate_ticket(
    config: Config, forge: Forge, agents: Agents, git: Git, ticket: Issue, integration: str, notes: Path | None,
    obs: Observer | None = None,
) -> Flow:
    obs = obs or NullObserver()
    branch = ticket_branch(config.branch_prefix, config.prd, ticket.number)
    worktree = config.state_root / f"prd-{config.prd}" / "worktrees" / f"ticket-{ticket.number}"

    forge.set_labels(ticket.number, add=(labels.IN_PROGRESS,), remove=(labels.QUEUED,))
    git.add_worktree(worktree, branch, integration)
    try:
        return (yield from _flow(config, forge, agents, git, ticket, integration, notes, obs, branch, worktree))
    finally:
        git.remove_worktree(worktree, branch)  # integrated, failed or blocked


def _flow(
    config: Config, forge: Forge, agents: Agents, git: Git, ticket: Issue, integration: str, notes: Path | None,
    obs: Observer, branch: str, worktree: Path,
) -> Flow:
    n = ticket.number

    def stop(status: TicketStatus, reason: str) -> TicketResult:
        return _stop(forge, git, worktree, branch, ticket, status, reason)

    session = yield _session(
        agents, _writer(config, "implementer", implementer_prompt(config, n, integration, notes), worktree, notes)
    )
    outcome = outcome_of(session)
    if outcome.kind is OutcomeKind.BLOCKED:
        return stop(TicketStatus.BLOCKED, outcome.reason or "no reason given")
    if outcome.kind is not OutcomeKind.DONE:
        return stop(TicketStatus.FAILED, "the implementer gave no usable outcome")

    pr: PullRequest | None = None
    rounds = 0  # verify-fix and gate-fix rounds together
    merge_rounds = 0  # merge-fix attempts, bounded apart from the rounds above
    gated = False  # the current code has a PASS: only a merge-fix keeps it
    while True:
        with obs.phase(n, "verify"):  # the first run and every re-run after a fix
            verify = yield lambda: check_verify(config.verify_commands, worktree)
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
                verdict, text, _ = yield lambda: _gate(config, agents, n, integration, worktree)
                if verdict is Verdict.UNPARSABLE:
                    return stop(
                        TicketStatus.FAILED,
                        "the gate gave no parsable verdict, even after a retry: the ticket was never judged.",
                    )
                gated = verdict is Verdict.PASS
            if gated:
                if forge.merge_pr(pr.number, method="merge", head_sha=head_sha):
                    forge.set_labels(n, add=(labels.INTEGRATED,), remove=(labels.IN_REVIEW,))
                    final.open_draft_after_first_merge(config, forge, integration)
                    return TicketResult(TicketStatus.INTEGRATED, f"ticket #{n} integrated")
                # Refused. Only a moved integration branch (a conflict) is the ticket's
                # to repair; any other refusal is a failure of the merge itself.
                if not _behind(git, worktree, integration):
                    return stop(TicketStatus.FAILED, f"the merge of PR #{pr.number} was refused")
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
                continue  # re-verify, push, merge: no new gate
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
