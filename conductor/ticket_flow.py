"""One ticket from queued to integrated, with fix rounds.

A failing verify or a ticket-gate FAIL starts a fix session (bounded by
`gate_fix_rounds`, shared by both kinds) and the ticket is re-verified and
re-gated. Exhausted rounds mean `ralph:failed:issue` with a diagnosis comment
and the PR left open. A session reporting "blocked" is an escalation
(`ralph:blocked`), never a failure. A gate verdict that cannot be parsed is
retried once with a fresh session and is never a PASS.
"""
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from conductor import labels
from conductor.config import Config
from conductor.findings import blocking_findings, tail
from conductor.markers import OutcomeKind, Verdict, outcome_of, verdict_of
from conductor.naming import ticket_branch
from conductor.ports import Agents, Forge, Git, Issue, PullRequest, SessionRequest, SessionResult
from conductor.prompts import fix_prompt, implementer_prompt, ticket_gate_prompt
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


def _gate(config: Config, agents: Agents, ticket: int, integration: str, cwd: Path) -> tuple[Verdict, str]:
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
            return verdict, result.text
        log.warning("ticket #%s: gate attempt %s gave no parsable verdict", ticket, attempt)
    return Verdict.UNPARSABLE, ""


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


def integrate_ticket(
    config: Config, forge: Forge, agents: Agents, git: Git, ticket: Issue, integration: str, notes: Path | None
) -> TicketResult:
    branch = ticket_branch(config.branch_prefix, config.prd, ticket.number)
    worktree = config.state_root / f"prd-{config.prd}" / "worktrees" / f"ticket-{ticket.number}"
    n = ticket.number

    forge.set_labels(n, add=(labels.IN_PROGRESS,), remove=(labels.QUEUED,))
    git.add_worktree(worktree, branch, integration)
    try:
        def stop(status: TicketStatus, reason: str) -> TicketResult:
            return _stop(forge, git, worktree, branch, ticket, status, reason)

        session = agents.start(
            _writer(config, "implementer", implementer_prompt(config, n, integration, notes), worktree, notes)
        )
        outcome = outcome_of(session)
        if outcome.kind is OutcomeKind.BLOCKED:
            return stop(TicketStatus.BLOCKED, outcome.reason or "no reason given")
        if outcome.kind is not OutcomeKind.DONE:
            return stop(TicketStatus.FAILED, "the implementer gave no usable outcome")

        pr: PullRequest | None = None
        rounds = 0
        while True:
            verify = check_verify(config.verify_commands, worktree)
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
                verdict, text = _gate(config, agents, n, integration, worktree)
                if verdict is Verdict.PASS:
                    if not forge.merge_pr(pr.number, method="merge", head_sha=head_sha):
                        return stop(TicketStatus.FAILED, f"the merge of PR #{pr.number} was refused")
                    forge.set_labels(n, add=(labels.INTEGRATED,), remove=(labels.IN_REVIEW,))
                    return TicketResult(TicketStatus.INTEGRATED, f"ticket #{n} integrated")
                if verdict is Verdict.UNPARSABLE:
                    return stop(
                        TicketStatus.FAILED,
                        "the gate gave no parsable verdict, even after a retry: the ticket was never judged.",
                    )
                findings, from_section = blocking_findings(text)
                fix = fix_prompt(config, n, integration, notes, reason="gate", context=findings, from_section=from_section)
                failure = f"the ticket gate failed after {rounds} fix round(s)."
            else:
                fix = fix_prompt(config, n, integration, notes, reason="verify", context=tail(verify.output))
                failure = f"verify still failing after {rounds} fix round(s).\n\n```\n{tail(verify.output, 40)}\n```"

            if rounds >= config.gate_fix_rounds:
                return stop(TicketStatus.FAILED, failure)
            rounds += 1
            fixed = outcome_of(agents.start(_writer(config, "fix", fix, worktree, notes)))
            if fixed.kind is OutcomeKind.BLOCKED:
                return stop(TicketStatus.BLOCKED, fixed.reason or "no reason given")
            if fixed.kind is not OutcomeKind.DONE:
                return stop(TicketStatus.FAILED, f"fix round {rounds} gave no usable outcome")
    finally:
        git.remove_worktree(worktree, branch)
