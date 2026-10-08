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
from conductor.naming import integration_branch
from conductor.observer import NullObserver, Observer
from conductor.ports import Agents, Blocker, Forge, Git, Issue
from conductor.preflight import Environment, preflight
from conductor.ticket_flow import integrate_ticket

EXIT_OK = 0
EXIT_STARTUP_ERROR = 1
EXIT_INCOMPLETE = 3

_HALTED = frozenset({labels.FAILED_ISSUE, labels.BLOCKED})


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
    """Integrate tickets one at a time, always from the current frontier.

    A failed or blocked ticket is left for a human; the loop carries on with
    whatever does not depend on it."""
    while True:
        tickets = forge.list_sub_issues(config.prd)
        ready = graph.dispatchable(tickets, blockers)
        if not ready:
            return _finish(tickets, blockers)
        integrate_ticket(config, forge, agents, git, ready[0], integration, notes, obs)


def _numbers(numbers: list[int]) -> str:
    return ", ".join(f"#{n}" for n in sorted(numbers))


def _finish(tickets: list[Issue], blockers: dict[int, list[Blocker]]) -> RunResult:
    """Why the frontier is empty: cascade, waiting, failures left, or done."""
    cascade = graph.halted_blockers(tickets, blockers, _HALTED)
    if cascade:
        blocking = sorted({n for found in cascade.values() for n in found})
        return RunResult(
            EXIT_INCOMPLETE,
            f"cascade: tickets {_numbers(list(cascade))} depend on failed or blocked {_numbers(blocking)}",
        )
    if any(labels.QUEUED in t.labels for t in tickets):
        return RunResult(EXIT_INCOMPLETE, "waiting on tickets or issues that are not done")
    halted = [t.number for t in tickets if _HALTED & t.labels]
    if halted:
        return RunResult(EXIT_INCOMPLETE, f"failed or blocked tickets left for a human: {_numbers(halted)}")
    return RunResult(EXIT_OK, "integrated")
