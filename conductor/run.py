"""The conductor entry point: `run(config, forge, agents, git)`.

Tracer bullet: one queued ticket from claim to merged-into-integration.
Later tickets extend this loop (frontier, parallelism, fix rounds, final PR)
through the same ports.
"""
from dataclasses import dataclass
from pathlib import Path

from conductor import graph, labels
from conductor.config import Config
from conductor.exploration import ensure_notes
from conductor.markers import OutcomeKind, Verdict, outcome_of, verdict_of
from conductor.naming import integration_branch, ticket_branch
from conductor.observer import NullObserver, Observer
from conductor.ports import Agents, Blocker, Forge, Git, Issue, SessionRequest
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


def run(
    config: Config,
    forge: Forge,
    agents: Agents,
    git: Git,
    env: Environment | None = None,
    observer: Observer | None = None,
) -> RunResult:
    obs = observer or NullObserver()  # observability (#64): status comment, run.log, last-run.md
    forge, agents = obs.wrap(forge, agents)
    obs.start()
    try:
        result = _run(config, forge, agents, git, env, obs)
    except BaseException as error:
        obs.finish(f"error ({type(error).__name__})")
        raise
    obs.finish(result.reason)
    return result


def _run(config: Config, forge: Forge, agents: Agents, git: Git, env: Environment | None, obs: Observer) -> RunResult:
    if env is not None:  # startup checks: nothing below runs if one fails
        failure = preflight(config, env)
        if failure is not None:
            return RunResult(EXIT_STARTUP_ERROR, f"preflight failed: {failure}")
    prd = forge.get_issue(config.prd)
    tickets = forge.list_sub_issues(config.prd)
    if not tickets:
        return RunResult(EXIT_STARTUP_ERROR, f"PRD #{config.prd} has no sub-issues")
    try:
        blockers = {t.number: forge.list_blockers(t.number) for t in tickets}
    except Exception as error:  # fail closed: unknown dependencies are never "none"
        return RunResult(EXIT_STARTUP_ERROR, f"could not read dependencies: {error}")
    cycle = graph.find_cycle(tickets, blockers)
    if cycle:
        names = ", ".join(f"#{n}" for n in cycle)
        return RunResult(EXIT_STARTUP_ERROR, f"dependency cycle among tickets: {names}")
    if not any(labels.QUEUED in t.labels for t in tickets):
        return RunResult(EXIT_STARTUP_ERROR, f"no {labels.QUEUED} ticket in PRD #{config.prd}")

    integration = integration_branch(config.branch_prefix, config.prd, prd.title)
    git.create_branch(integration, config.base_branch)
    notes = ensure_notes(config, agents)
    return _work_frontier(config, forge, agents, git, blockers, integration, notes, obs)


def _work_frontier(
    config: Config, forge: Forge, agents: Agents, git: Git, blockers: dict[int, list[Blocker]], integration: str,
    notes: Path | None,
    obs: Observer,
) -> RunResult:
    """Integrate tickets one at a time, always from the current frontier."""
    while True:
        tickets = forge.list_sub_issues(config.prd)
        ready = graph.dispatchable(tickets, blockers)
        if not ready:
            if any(labels.QUEUED in t.labels for t in tickets):
                return RunResult(EXIT_INCOMPLETE, "waiting on tickets or issues that are not done")
            return RunResult(EXIT_OK, "integrated")
        result = _integrate_ticket(config, forge, agents, git, ready[0], integration, notes, obs)
        if result.exit_code != EXIT_OK:
            return result


def _fail(forge: Forge, ticket: Issue, label: str, reason: str) -> RunResult:
    forge.set_labels(ticket.number, add=(label,), remove=(labels.IN_PROGRESS, labels.IN_REVIEW))
    return RunResult(EXIT_INCOMPLETE, reason)


def _integrate_ticket(
    config: Config, forge: Forge, agents: Agents, git: Git, ticket: Issue, integration: str,
    notes: Path | None,
    obs: Observer,
) -> RunResult:
    branch = ticket_branch(config.branch_prefix, config.prd, ticket.number)
    worktree = config.state_root / f"prd-{config.prd}" / "worktrees" / f"ticket-{ticket.number}"

    forge.set_labels(ticket.number, add=(labels.IN_PROGRESS,), remove=(labels.QUEUED,))
    git.add_worktree(worktree, branch, integration)
    try:
        session = agents.start(
            SessionRequest(
                role="implementer",
                prompt=implementer_prompt(config, ticket.number, integration, notes),
                cwd=worktree,
                model=config.model,
                timeout=config.session_timeout,
                add_dirs=(notes.parent,) if notes else (),
            )
        )
        outcome = outcome_of(session)
        if outcome.kind is OutcomeKind.BLOCKED:
            return _fail(forge, ticket, labels.BLOCKED, f"ticket #{ticket.number} blocked: {outcome.reason}")
        if outcome.kind is not OutcomeKind.DONE:
            return _fail(forge, ticket, labels.FAILED_ISSUE, f"ticket #{ticket.number}: no usable outcome")

        with obs.phase(ticket.number, "verify"):
            verified = run_verify(config.verify_commands, worktree)
        if not verified:
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
        return RunResult(EXIT_OK, f"ticket #{ticket.number} integrated")
    finally:
        git.remove_worktree(worktree, branch)
