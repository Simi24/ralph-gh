"""The conductor entry point: `run(config, forge, agents, git)`.

Tracer bullet: one queued ticket from claim to merged-into-integration.
Later tickets extend this loop (frontier, parallelism, fix rounds, final PR)
through the same ports.
"""
from dataclasses import dataclass

from conductor import labels
from conductor.config import Config
from conductor.markers import OutcomeKind, Verdict, outcome_of, verdict_of
from conductor.naming import integration_branch, ticket_branch
from conductor.ports import Agents, Forge, Git, Issue, SessionRequest
from conductor.preflight import Environment, preflight
from conductor.prompts import implementer_prompt, ticket_gate_prompt
from conductor.verify import run_verify

EXIT_OK = 0
EXIT_STARTUP_ERROR = 1
EXIT_INCOMPLETE = 3


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    reason: str


def run(config: Config, forge: Forge, agents: Agents, git: Git, env: Environment | None = None) -> RunResult:
    if env is not None:  # startup checks: nothing below runs if one fails
        failure = preflight(config, env)
        if failure is not None:
            return RunResult(EXIT_STARTUP_ERROR, f"preflight failed: {failure}")
    prd = forge.get_issue(config.prd)
    queued = [t for t in forge.list_sub_issues(config.prd) if labels.QUEUED in t.labels]
    if len(queued) != 1:
        return RunResult(EXIT_STARTUP_ERROR, f"expected exactly one {labels.QUEUED} ticket, found {len(queued)}")

    integration = integration_branch(config.branch_prefix, config.prd, prd.title)
    git.create_branch(integration, config.base_branch)
    return _integrate_ticket(config, forge, agents, git, queued[0], integration)


def _fail(forge: Forge, ticket: Issue, label: str, reason: str) -> RunResult:
    forge.set_labels(ticket.number, add=(label,), remove=(labels.IN_PROGRESS, labels.IN_REVIEW))
    return RunResult(EXIT_INCOMPLETE, reason)


def _integrate_ticket(
    config: Config, forge: Forge, agents: Agents, git: Git, ticket: Issue, integration: str
) -> RunResult:
    branch = ticket_branch(config.branch_prefix, config.prd, ticket.number)
    worktree = config.state_root / f"prd-{config.prd}" / "worktrees" / f"ticket-{ticket.number}"

    forge.set_labels(ticket.number, add=(labels.IN_PROGRESS,), remove=(labels.QUEUED,))
    git.add_worktree(worktree, branch, integration)
    try:
        session = agents.start(
            SessionRequest(
                role="implementer",
                prompt=implementer_prompt(config, ticket.number, integration),
                cwd=worktree,
                model=config.model,
                timeout=config.session_timeout,
            )
        )
        outcome = outcome_of(session)
        if outcome.kind is OutcomeKind.BLOCKED:
            return _fail(forge, ticket, labels.BLOCKED, f"ticket #{ticket.number} blocked: {outcome.reason}")
        if outcome.kind is not OutcomeKind.DONE:
            return _fail(forge, ticket, labels.FAILED_ISSUE, f"ticket #{ticket.number}: no usable outcome")

        if not run_verify(config.verify_commands, worktree):
            return _fail(forge, ticket, labels.FAILED_ISSUE, f"ticket #{ticket.number}: verify failed")

        git.push(worktree, branch)
        head_sha = git.head_sha(worktree)
        pr = forge.create_pr(
            head=branch,
            base=integration,
            title=f"{ticket.title} (#{ticket.number})",
            body=f"Ticket #{ticket.number} of PRD #{config.prd}.",
        )
        forge.set_labels(ticket.number, add=(labels.IN_REVIEW,), remove=(labels.IN_PROGRESS,))

        gate = agents.start(
            SessionRequest(
                role="ticket-gate",
                prompt=ticket_gate_prompt(config, ticket.number, integration),
                cwd=worktree,
                agent=config.ticket_gate_agent,
                timeout=config.session_timeout,
            )
        )
        if verdict_of(gate) is not Verdict.PASS:
            return _fail(forge, ticket, labels.FAILED_ISSUE, f"ticket #{ticket.number}: gate did not pass")

        if not forge.merge_pr(pr.number, method="merge", head_sha=head_sha):
            return _fail(forge, ticket, labels.FAILED_ISSUE, f"ticket #{ticket.number}: merge refused")
        forge.set_labels(ticket.number, add=(labels.INTEGRATED,), remove=(labels.IN_REVIEW,))
        return RunResult(EXIT_OK, "integrated")
    finally:
        git.remove_worktree(worktree, branch)
