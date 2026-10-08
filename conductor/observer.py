"""Observability facade: status comment, run.log, last-run.md, heartbeat, timings.

`run()` takes an optional Observer and calls `wrap`, `start`, `phase` and
`finish`; everything else happens in the decorators in observed.py.
`NullObserver` is the default and does nothing.
"""
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from conductor.clock import Clock, SystemClock
from conductor.heartbeat import Heartbeat
from conductor.last_run import LastRun
from conductor.observed import ObservedAgents, ObservedForge
from conductor.run_log import RunLog
from conductor.status_comment import StatusComment
from conductor.timings import Timings, phase_of_role

log = logging.getLogger("conductor")


class NullObserver:
    def wrap(self, forge: Any, agents: Any) -> tuple[Any, Any]:
        return forge, agents

    def start(self) -> None: ...
    def event(self, ticket: int | None, text: str) -> None: ...
    def finish(self, reason: str) -> None: ...

    @contextmanager
    def phase(self, ticket: int, phase: str) -> Iterator[None]:
        yield

    @contextmanager
    def session(self, ticket: int | None, role: str) -> Iterator[None]:
        yield


class Observer(NullObserver):
    def __init__(
        self, forge: Any, *, prd: int, state_root: Path, repo_root: Path, clock: Clock | None = None, echo: bool = False
    ) -> None:
        self._clock = clock or SystemClock()
        self._prd = prd
        self._session = f"ralph-{int(time.time())}"
        self._status = StatusComment(forge, prd, self._clock)
        self._run_log = RunLog(state_root / "run.log", self._clock, echo=echo)
        self._last_run = LastRun(state_root / "last-run.md", self._clock, self._session, prd, str(repo_root))
        self._timings = Timings()
        self.heartbeat = Heartbeat(lambda text: self.event(None, text), self._clock)
        self._finished = False

    def wrap(self, forge: Any, agents: Any) -> tuple[Any, Any]:
        return ObservedForge(forge, self), ObservedAgents(agents, self)

    def start(self) -> None:
        self._run_log.attach()
        self._run_log.banner(f"ralph-gh session: {self._session}", f"PRD: #{self._prd}")
        self._last_run.update({})
        self.heartbeat.start()

    def event(self, ticket: int | None, text: str) -> None:
        who = "PRD" if ticket is None else f"#{ticket}"
        log.info(f"[status] {who} {text}")
        self._status.post(f"{who} {text}")

    @contextmanager
    def phase(self, ticket: int, phase: str) -> Iterator[None]:
        self.event(ticket, f"{phase} started")
        with self._measure(ticket, phase):
            yield

    @contextmanager
    def session(self, ticket: int | None, role: str) -> Iterator[None]:
        label = f"{role} {'PRD' if ticket is None else f'#{ticket}'}"
        self.heartbeat.session_started(label)
        try:
            if ticket is None:
                yield
            else:
                with self._measure(ticket, phase_of_role(role)):
                    yield
        finally:
            self.heartbeat.session_ended(label)

    @contextmanager
    def _measure(self, ticket: int, phase: str) -> Iterator[None]:
        try:
            with self._timings.measure(ticket, phase):
                yield
        finally:
            self._last_run.update(self._timings.snapshot())

    def finish(self, reason: str) -> None:
        if self._finished:
            return
        self._finished = True
        self.heartbeat.stop()
        self.event(None, f"run ended: {reason}")
        self._last_run.update(self._timings.snapshot(), reason)
        log.info(f"ralph-gh exited: {reason}")
        self._run_log.detach()
