"""The conductor entry point: `run(config, forge, agents, git)`.

Tracer bullet: one queued ticket from claim to merged-into-integration.
Later tickets extend this loop (frontier, parallelism, fix rounds, final PR)
through the same ports.
"""
import time
from pathlib import Path

from conductor import final, graph, labels
from conductor.config import Config
from conductor.exploration import ensure_notes
from conductor.lock import RunLock
from conductor.naming import integration_branch
from conductor.observer import NullObserver, Observer
from conductor.ports import Agents, Blocker, Forge, Git, Issue
from conductor.preflight import Environment, preflight
from conductor.result import EXIT_INCOMPLETE, EXIT_OK, EXIT_STARTUP_ERROR, RunResult
from conductor.scheduler import work_frontier
from conductor.usage_limit import Sleep, UsageLimitHit, exit_reason, guarded

_HALTED = frozenset({labels.FAILED_ISSUE, labels.BLOCKED})

def run(
    config: Config,
    forge: Forge,
    agents: Agents,
    git: Git,
    env: Environment | None = None,
    observer: Observer | None = None,
    sleep: Sleep = time.sleep,
) -> RunResult:
    lock = RunLock(config.state_root / "lock")
    if not lock.acquire():  # before anything mutates: not the board, not the state directory
        holder = f" (pid {lock.holder})" if lock.holder else ""
        return RunResult(EXIT_STARTUP_ERROR, f"another conductor is already running for this repo{holder}")
    try:
        return _run_locked(config, forge, agents, git, env, observer, sleep)
    finally:
        lock.release()


def _run_locked(
    config: Config, forge: Forge, agents: Agents, git: Git, env: Environment | None, observer: Observer | None,
    sleep: Sleep,
) -> RunResult:
    obs = observer or NullObserver()  # observability (#64): status comment, run.log, last-run.md
    forge, agents = obs.wrap(forge, agents)
    obs.start()
    try:
        try:
            result = _run(config, forge, agents, git, env, obs, sleep)
        except UsageLimitHit as hit:  # usage limit (#62): an incomplete run, never a failure
            result = RunResult(EXIT_INCOMPLETE, exit_reason(hit.description))
    except BaseException as error:
        obs.finish(f"error ({type(error).__name__})")
        raise
    obs.finish(result.reason)
    return result


def _run(
    config: Config, forge: Forge, agents: Agents, git: Git, env: Environment | None, obs: Observer, sleep: Sleep
) -> RunResult:
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
    notes = guarded(config, sleep, lambda: ensure_notes(config, agents))
    frontier = _work_frontier(config, forge, agents, git, blockers, integration, notes, obs, sleep)
    if frontier.exit_code != EXIT_OK:
        return frontier
    return guarded(config, sleep, lambda: final.run_final(config, forge, agents, git, integration))


def _work_frontier(
    config: Config, forge: Forge, agents: Agents, git: Git, blockers: dict[int, list[Blocker]], integration: str,
    notes: Path | None,
    obs: Observer, sleep: Sleep,
) -> RunResult:
    """Integrate tickets, up to `parallel` at a time, always from the current frontier.

    A failed or blocked ticket is left for a human; the loop carries on with
    whatever does not depend on it."""
    work_frontier(config, forge, agents, git, blockers, integration, notes, obs, sleep)
    return _finish(forge.list_sub_issues(config.prd), blockers)


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
