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
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conductor import graph
from conductor.config import Config
from conductor.observer import Observer
from conductor.ports import Agents, Blocker, Forge, Git, Issue
from conductor.ticket_flow import Flow, integrate_ticket

POLL_SECONDS = 1.0  # how long the conductor waits before it flushes observer lines again


@dataclass
class _Running:
    flow: Flow


def work_frontier(
    config: Config, forge: Forge, agents: Agents, git: Git, blockers: dict[int, list[Blocker]], integration: str,
    notes: Path | None, obs: Observer,
) -> None:
    """Dispatch and drive tickets until nothing is running and nothing can be dispatched."""
    finished: queue.Queue[tuple[int, Future[Any]]] = queue.Queue()
    running: dict[int, _Running] = {}
    dispatched: set[int] = set()
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

    def dispatch(ticket: Issue) -> None:
        dispatched.add(ticket.number)
        flow = integrate_ticket(config, forge, agents, git, ticket, integration, notes, obs)
        running[ticket.number] = _Running(flow)
        advance(ticket.number)  # claims the ticket and starts its first job

    try:
        while True:
            obs.flush()
            if len(running) < config.parallel:
                tickets = forge.list_sub_issues(config.prd)
                ready = [t for t in graph.dispatchable(tickets, blockers) if t.number not in dispatched]
                for ticket in ready[: config.parallel - len(running)]:
                    dispatch(ticket)
            if not running:
                return
            try:
                number, future = finished.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            error = future.exception()
            advance(number, None if error else future.result(), error)
    finally:
        pool.shutdown(wait=True)  # in-flight sessions end before their worktrees are removed
        for state in running.values():
            _close(state.flow)


def _close(flow: Generator[Any, Any, Any]) -> None:
    try:
        flow.close()  # runs the flow's cleanup: the worktree is removed
    except Exception:
        pass
