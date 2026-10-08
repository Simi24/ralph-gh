"""The frontier scheduler: up to `parallel` ticket flows at once (critical path).

Threading rule: ONLY the conductor thread (the one that calls `work_frontier`)
touches the forge or git. A ticket flow (ticket_flow.py) is a generator that
yields `Job`s, which are the steps that merely run a session or a local
command. Those run in worker threads and return their result, nothing more.
Each finished job is sent back into its generator on the conductor thread,
which then does the next label, PR, merge or comment itself. Merges are
therefore serial by construction.

A ticket is dispatched at most once per run, however the board looks later.
"""
import queue
import time
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conductor import graph, reconcile, stopping, usage_limit
from conductor.config import Config
from conductor.observer import Observer
from conductor.ports import Agents, Blocker, Forge, Git, Issue, PullRequest
from conductor.stopping import StopState
from conductor.ticket_flow import Flow, integrate_ticket

POLL_SECONDS = 1.0  # how long the conductor waits before it flushes observer lines again


@dataclass
class _Running:
    flow: Flow
    ticket: Issue


def work_frontier(
    config: Config, forge: Forge, agents: Agents, git: Git, blockers: dict[int, list[Blocker]], integration: str,
    notes: Path | None, obs: Observer, stop: StopState, sleep: usage_limit.Sleep = time.sleep,
) -> None:
    """Dispatch and drive tickets until nothing is running and nothing can be dispatched,
    or until `stop` says otherwise (a drain dispatches nothing new, an immediate stop
    abandons everything in flight; see stopping.py)."""
    finished: queue.Queue[tuple[int, Future[Any]]] = queue.Queue()
    running: dict[int, _Running] = {}
    dispatched: set[int] = set()
    limit: str | None = None  # usage limit (#62): the reset description while one is in force
    pool = ThreadPoolExecutor(max_workers=config.parallel, thread_name_prefix="ralph-session")

    def submit(number: int, job: Any) -> None:
        future = pool.submit(job)
        future.add_done_callback(lambda f: finished.put((number, f)))

    def advance(number: int, send: Any = None, error: BaseException | None = None) -> None:
        """Resume a flow with a job's result (or error) until it yields the next job or ends."""
        flow = running[number].flow
        try:
            job = flow.throw(error) if error is not None else flow.send(send)
        except StopIteration:
            del running[number]
            return
        submit(number, job)

    def park(number: int) -> None:
        """usage limit (#62): end the flow (its worktree goes) and give the ticket back."""
        state = running.pop(number)
        _close(state.flow)
        if usage_limit.park_ticket(config, forge, number, integration, limit or usage_limit.UNKNOWN_RESET):
            dispatched.discard(number)  # queued again: a resumed run may dispatch it once more

    def dispatch(ticket: Issue, resume: PullRequest | None) -> None:
        dispatched.add(ticket.number)
        flow = integrate_ticket(config, forge, agents, git, ticket, integration, notes, obs, stop.sessions, resume)
        running[ticket.number] = _Running(flow, ticket)
        advance(ticket.number)  # claims the ticket and starts its first job

    resumes = reconcile.resumable(config, forge, integration, forge.list_sub_issues(config.prd))
    try:
        while True:
            obs.flush()
            stop.poll_file()  # --- stop hook: STOP file ---
            if stop.immediate:  # --- stop hook: abandon in-flight work (parked in `finally`) ---
                return
            if limit is None and not stop.draining and len(running) < config.parallel:  # a drain dispatches nothing new
                tickets = forge.list_sub_issues(config.prd)
                ready = [(t, pr) for t, pr in resumes if t.number not in dispatched]
                ready += [(t, None) for t in graph.dispatchable(tickets, blockers) if t.number not in dispatched]
                for ticket, resume in ready[: config.parallel - len(running)]:
                    dispatch(ticket, resume)
            if not running:
                if limit is None or stop.draining:  # a stop ends the run, whatever the limit says
                    return
                # usage limit (#62): everything is parked; exit, or wait and resume
                if not config.wait_for_reset:
                    raise usage_limit.UsageLimitHit(limit)
                obs.event(None, f"usage limit hit ({limit}), waiting {config.usage_wait_seconds}s before resuming")
                if not usage_limit.wait_for_reset(config, sleep, stop.should_stop):  # --- stop hook: interrupts the wait ---
                    return
                limit = None
                continue
            try:
                number, future = finished.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            if stop.immediate:  # --- stop hook: a result that arrives now is never acted on ---
                return
            error = future.exception()
            hit = None if error else usage_limit.limited_result(future.result())
            if hit is not None and limit is None:  # usage limit (#62): stop dispatching
                limit = usage_limit.describe(hit)
            if limit is not None:  # also parks flows whose job ended after the hit
                park(number)
                continue
            advance(number, None if error else future.result(), error)
    finally:
        pool.shutdown(wait=True)  # in-flight sessions end before their worktrees are removed
        abandoned = [state.ticket for state in running.values()]
        for state in running.values():
            _close(state.flow)
        stopping.requeue_in_flight(stop, config, forge, integration, abandoned)  # --- stop hook: no-op unless immediate ---


def _close(flow: Generator[Any, Any, Any]) -> None:
    try:
        flow.close()  # runs the flow's cleanup: the worktree is removed
    except Exception:
        pass
