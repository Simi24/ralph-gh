"""Stopping a run: drain, or stop immediately (critical path: signals, labels).

One `StopState` per run is the whole API for anything that wants the run to
stop, the STOP file, `ralph-gh stop`, signals, and later the usage-limit
logic (#62):

    stop.request_drain(reason, exit_code)      nothing new is dispatched; in-flight tickets finish
    stop.request_immediate(reason, exit_code)  kill every session, requeue in-flight tickets

Order rule: the flag is set BEFORE any session is killed and long before any
label is touched, so nothing that reads `stop.immediate` can see "not
stopping" while labels move.

The scheduler (scheduler.py) honours the state; `StopAwareAgents` makes
sessions that end after an immediate stop raise `Stopped` instead of
returning a result nobody may act on; `requeue_in_flight` does the label
work once the flows are closed.
"""
import logging
import os
import threading
from pathlib import Path
from typing import Any

from conductor import usage_limit
from conductor.config import Config
from conductor.ports import Forge, Issue, SessionRequest, SessionResult
from conductor.result import EXIT_OK, RunResult
from conductor.sessions import SessionRegistry, Stopped

log = logging.getLogger("conductor")

STOP_FILE = "STOP"
REASON_DRAIN = "stopped by operator"
REASON_IMMEDIATE = "stopped by operator (immediate)"


class StopState:
    def __init__(self, stop_file: Path | None = None, sessions: SessionRegistry | None = None) -> None:
        self.sessions = sessions or SessionRegistry()
        self._stop_file = stop_file
        self._lock = threading.RLock()  # re-entrant: signal handlers run on the thread that may hold it
        self._draining = False
        self._immediate = False
        self._reason = REASON_DRAIN
        self._exit_code = EXIT_OK

    @property
    def draining(self) -> bool:
        """True once any stop was requested (an immediate stop drains too)."""
        return self._draining or self._immediate

    @property
    def immediate(self) -> bool:
        return self._immediate

    def request_drain(self, reason: str = REASON_DRAIN, exit_code: int = EXIT_OK) -> None:
        with self._lock:
            if self.draining:
                return  # the first reason wins
            self._draining = True
            self._reason, self._exit_code = reason, exit_code
        log.info(f"[stop] {reason}: no new tickets; in-flight tickets finish. Send SIGINT again to stop now.")

    def request_immediate(self, reason: str = REASON_IMMEDIATE, exit_code: int = EXIT_OK) -> None:
        with self._lock:
            already = self._immediate
            self._immediate = True  # the flag first, then the kill
            if not already:
                self._reason, self._exit_code = reason, exit_code
        if not already:
            log.info(f"[stop] {reason}: killing every session and requeuing in-flight tickets")
            self.sessions.kill_all()

    def escalate(self) -> None:
        """What a SIGINT does: the first drains, the next stops immediately."""
        with self._lock:
            drained = self.draining
        if drained:
            self.request_immediate()
        else:
            self.request_drain()

    def result(self) -> RunResult | None:
        """The run result a stop produces, or None if no stop was requested."""
        with self._lock:
            return RunResult(self._exit_code, self._reason) if self.draining else None

    def should_stop(self) -> bool:
        """For waits (usage-limit wait): look at the STOP file too, then say whether to stop waiting."""
        self.poll_file()
        return self.draining

    # --- the STOP file ---

    def poll_file(self) -> None:
        """Consume the STOP file if present and drain. Conductor thread, once per loop pass."""
        if self._stop_file is None:
            return
        try:
            self._stop_file.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            log.warning(f"could not consume {self._stop_file}: {error}")
        self.request_drain()

    def clear_stale_file(self) -> None:
        """Startup, after the run lock is held: a STOP file from an earlier run must not stop this one."""
        if self._stop_file is not None:
            self._stop_file.unlink(missing_ok=True)


def write_stop_file(state_root: Path) -> None:
    """`ralph-gh stop`: ask the running conductor of this repo to drain."""
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / STOP_FILE).write_text(f"{os.getpid()}\n", encoding="utf-8")


class StopAwareAgents:
    """Agents decorator: no session starts, and none returns, after an immediate stop."""

    def __init__(self, inner: Any, stop: StopState) -> None:
        self._inner = inner
        self._stop = stop

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def start(self, request: SessionRequest) -> SessionResult:
        if self._stop.immediate:
            raise Stopped("the run is stopping")
        result = self._inner.start(request)
        if self._stop.immediate:
            raise Stopped("the run is stopping")  # a killed session's output is never acted on
        return result


def requeue_in_flight(stop: StopState, config: Config, forge: Forge, integration: str, tickets: list[Issue]) -> None:
    """After an immediate stop (a no-op otherwise): in-flight tickets with no open PR go
    back to `ralph:queued` with a comment; tickets with an open PR stay `ralph:in-review`.
    Never raises, and an unreadable PR state leaves the ticket untouched (fail closed)."""
    if not stop.immediate:
        return
    for ticket in tickets:  # the same park path as a usage limit (usage_limit.park_ticket)
        try:
            usage_limit.park_ticket(config, forge, ticket.number, integration, "immediate stop", "was stopped by the operator")
        except Exception as error:  # noqa: BLE001 - one ticket must not strand the others
            log.warning(f"could not park ticket #{ticket.number} after the stop: {error}")
