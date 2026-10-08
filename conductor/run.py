"""The conductor entry point: `run(config, forge, agents, git)`.

Takes the per-repo run lock, then in order: preflight (when an `env` is given),
reads the PRD board and refuses a missing, cyclic or unreadable one, reconciles
leftover labels with their PRs (a resumed run starts from the real board), creates
the integration branch and the exploration notes, drives the frontier
(scheduler.py: up to `parallel` ticket flows at once), and, when every open
ticket is integrated, runs the final review and merge (final.py). Stop requests
(stopping.py) and usage limits (usage_limit.py) end the run early without
failing any ticket. The result carries the exit code and the reason line.
"""
import time

from conductor import final, graph, labels, reconcile
from conductor.config import Config
from conductor.exploration import ensure_notes
from conductor.final import not_started_reason, numbers
from conductor.lock import RunLock
from conductor.naming import integration_branch
from conductor.observer import NullObserver, Observer
from conductor.ports import Agents, Blocker, Forge, Git, Issue
from conductor.preflight import Environment, preflight
from conductor.result import EXIT_INCOMPLETE, EXIT_OK, EXIT_STARTUP_ERROR, RunResult
from conductor.scheduler import work_frontier
from conductor.sessions import Stopped
from conductor.stopping import STOP_FILE, StopAwareAgents, StopState
from conductor.usage_limit import Sleep, UsageLimitHit, exit_reason, guarded

_HALTED = frozenset({labels.FAILED_ISSUE, labels.FAILED_SYSTEMIC, labels.BLOCKED})


def run(
    config: Config,
    forge: Forge,
    agents: Agents,
    git: Git,
    env: Environment | None = None,
    observer: Observer | None = None,
    stop: StopState | None = None,
    sleep: Sleep = time.sleep,
) -> RunResult:
    stop = stop or StopState(config.state_root / STOP_FILE)
    lock = RunLock(config.state_root / "lock")
    if not lock.acquire():  # before anything mutates: not the board, not the state directory
        holder = f" (pid {lock.holder})" if lock.holder else ""
        return RunResult(EXIT_STARTUP_ERROR, f"another conductor is already running for this repo{holder}")
    try:
        stop.clear_stale_file()  # only with the lock held: never eat a live run's STOP file
        return _run_locked(config, forge, agents, git, env, observer, stop, sleep)
    finally:
        lock.release()


def _run_locked(
    config: Config, forge: Forge, agents: Agents, git: Git, env: Environment | None, observer: Observer | None,
    stop: StopState, sleep: Sleep,
) -> RunResult:
    obs = observer or NullObserver()  # observability (#64): status comment, run.log, last-run.md
    forge, agents = obs.wrap(forge, agents)
    agents = StopAwareAgents(agents, stop)
    obs.start()
    try:
        try:
            result = _run(config, forge, agents, git, env, obs, stop, sleep)
        except Stopped:  # a session ended or started after an immediate stop
            result = stop.result() or RunResult(EXIT_INCOMPLETE, "stopped")
        except UsageLimitHit as hit:  # usage limit (#62): an incomplete run, never a failure
            result = stop.result() or RunResult(EXIT_INCOMPLETE, exit_reason(hit.description))  # a stop wins
    except BaseException as error:
        obs.finish(f"error ({type(error).__name__})")
        raise
    obs.finish(result.reason)
    return result


def _run(
    config: Config, forge: Forge, agents: Agents, git: Git, env: Environment | None, obs: Observer, stop: StopState,
    sleep: Sleep,
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
    integration = integration_branch(config.branch_prefix, config.prd, prd.title)
    reconcile.reconcile_board(config, forge, integration, tickets)  # a resumed run starts from the real board
    tickets = forge.list_sub_issues(config.prd)
    if not any({labels.QUEUED, labels.IN_REVIEW, labels.INTEGRATED} & t.labels for t in tickets):
        return RunResult(EXIT_STARTUP_ERROR, f"no {labels.QUEUED} ticket in PRD #{config.prd}")

    git.create_branch(integration, config.base_branch)
    notes = guarded(config, sleep, lambda: ensure_notes(config, agents), stop.should_stop)
    work_frontier(config, forge, agents, git, blockers, integration, notes, obs, stop, sleep)
    frontier = _finish(forge.list_sub_issues(config.prd), blockers)  # a failed or blocked ticket is left for a human
    stop.poll_file()  # a STOP file dropped during the last ticket
    stopped = stop.result()
    if stopped is not None:  # the board is left for a later run; no final PR work after a stop
        return stopped
    if frontier.exit_code != EXIT_OK:
        return frontier
    return guarded(config, sleep, lambda: final.run_final(config, forge, agents, git, integration, notes, obs), stop.should_stop)


def _finish(tickets: list[Issue], blockers: dict[int, list[Blocker]]) -> RunResult:
    """Why the frontier is empty: cascade, waiting, failures left, or done."""
    cascade = graph.halted_blockers(tickets, blockers, _HALTED)
    if cascade:
        blocking = sorted({n for found in cascade.values() for n in found})
        return RunResult(
            EXIT_INCOMPLETE,
            f"cascade: tickets {numbers(list(cascade))} depend on failed or blocked {numbers(blocking)}",
        )
    live = [t for t in tickets if t.state == "open"]  # a closed sub-issue is a human's call: ignored
    if any(labels.QUEUED in t.labels for t in live):
        return RunResult(EXIT_INCOMPLETE, "waiting on tickets or issues that are not done")
    halted = [t.number for t in live if _HALTED & t.labels]
    if halted:
        return RunResult(EXIT_INCOMPLETE, f"failed or blocked tickets left for a human: {numbers(halted)}")
    not_started = not_started_reason(tickets)  # e.g. an open sub-issue that never opted in
    if not_started is not None:
        return RunResult(EXIT_INCOMPLETE, not_started)
    return RunResult(EXIT_OK, "integrated")
