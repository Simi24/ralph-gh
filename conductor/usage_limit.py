"""Usage limits: a hit is a pause, never a failure (critical path: label writes).

Detection lives in the real `claude` adapter, which sets `SessionResult.usage_limit`
(the CLI's `is_error` first, then the documented wording). This module decides
what a hit means for the run:

- nothing is labelled `ralph:failed:*`;
- new dispatches stop; every in-flight ticket is parked: back to
  `ralph:queued` when it has no open PR, left `ralph:in-review` when it has one;
- then the run exits ("usage limit - <reset>") or, with `wait_for_reset`,
  sleeps `usage_wait_seconds` (in short slices) and resumes.
"""
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from conductor import labels
from conductor.config import Config
from conductor.naming import ticket_branch
from conductor.ports import Forge, SessionResult

log = logging.getLogger("conductor")

UNKNOWN_RESET = "reset time unknown (not stated in session output)"
SLICE_SECONDS = 5.0  # a wait is slept in slices so a stop request is honored during it

Sleep = Callable[[float], None]
T = TypeVar("T")


class UsageLimitHit(Exception):
    """A session hit the account's usage limit and the run must pause or end."""

    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


def describe(result: SessionResult) -> str:
    return result.usage_reset.strip() or UNKNOWN_RESET


def exit_reason(description: str) -> str:
    return f"usage limit — {description}"


def limited_result(value: Any) -> SessionResult | None:
    """The usage-limited session inside a job's return value (a result, or a tuple holding one)."""
    for item in value if isinstance(value, tuple) else (value,):
        if isinstance(item, SessionResult) and item.usage_limit:
            return item
    return None


def wait_for_reset(config: Config, sleep: Sleep, interrupted: Callable[[], bool] = lambda: False) -> bool:
    """Sleep `usage_wait_seconds`; False if `interrupted()` cut the wait short."""
    remaining = float(config.usage_wait_seconds)
    while remaining > 0:
        if interrupted():
            return False
        step = min(SLICE_SECONDS, remaining)
        sleep(step)
        remaining -= step
    return True


def guarded(config: Config, sleep: Sleep, action: Callable[[], T]) -> T:
    """Run a step outside the frontier; on a usage limit wait and retry, or let `UsageLimitHit` out."""
    while True:
        try:
            return action()
        except UsageLimitHit as hit:
            if not config.wait_for_reset:
                raise
            log.warning("[usage-limit] waiting %ss (%s) before resuming", config.usage_wait_seconds, hit.description)
            wait_for_reset(config, sleep)


def _retry_once(action: Callable[[], None]) -> bool:
    for _ in range(2):
        try:
            action()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def park_ticket(config: Config, forge: Forge, number: int, integration: str, description: str) -> bool:
    """Give a ticket back after a usage limit; True if it went back to `ralph:queued`.

    Without an open PR it is requeued. With one it stays `ralph:in-review`.
    If the PR cannot be read the labels are left alone: guessing wrong is worse."""
    try:
        pr = forge.find_pr(head=ticket_branch(config.branch_prefix, config.prd, number), base=integration)
    except Exception as error:  # noqa: BLE001
        log.warning("[usage-limit] ticket #%s: cannot tell whether a PR is open (%s); labels left as they are", number, error)
        return False
    if pr is not None:
        _retry_once(lambda: forge.set_labels(number, add=(labels.IN_REVIEW,), remove=(labels.IN_PROGRESS,)))
        note = f"PR #{pr.number} stays {labels.IN_REVIEW}"
        requeued = False
    else:
        _retry_once(
            lambda: forge.set_labels(number, add=(labels.QUEUED,), remove=(labels.IN_PROGRESS, labels.IN_REVIEW))
        )
        note = "requeued for retry"
        requeued = True
    _retry_once(
        lambda: forge.comment(number, f"ralph-gh: ticket #{number} hit a usage limit ({description}), {note} (not a failure).")
    )
    log.warning("[usage-limit] ticket #%s: %s (%s), not a failure", number, note, description)
    return requeued
