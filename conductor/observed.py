"""Observing decorators for the Forge and Agents ports.

They turn what the conductor already does (label moves, PR creation, merges,
sessions) into status lines, log lines and timings, so run.py needs no
per-event calls. Methods they do not override pass straight through.
"""
import re
import time
from typing import TYPE_CHECKING, Any

from conductor import labels
from conductor.ports import PullRequest, SessionRequest, SessionResult

if TYPE_CHECKING:
    from conductor.observer import Observer

_TICKET_DIR = re.compile(r"^ticket-(\d+)$")
_TICKET_BRANCH = re.compile(r"-ticket-(\d+)$")

_LABEL_EVENTS = (
    (labels.IN_PROGRESS, "dispatched"),
    (labels.IN_REVIEW, "in review"),
    (labels.INTEGRATED, "integrated"),
    (labels.BLOCKED, "blocked"),
    (labels.FAILED_ISSUE, "failed"),
    (labels.FAILED_SYSTEMIC, "failed (systemic)"),
    (labels.QUEUED, "queued"),
)


class ObservedForge:
    def __init__(self, inner: Any, observer: "Observer") -> None:
        self._inner = inner
        self._obs = observer
        self._tickets: dict[int, int] = {}  # PR number -> ticket, for ticket-branch PRs only

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def set_labels(self, number: int, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> None:
        self._inner.set_labels(number, add=add, remove=remove)
        for label, event in _LABEL_EVENTS:
            if label in add:
                self._obs.event(number, event)

    def create_pr(self, *, head: str, base: str, title: str, body: str, draft: bool = False) -> PullRequest:
        pr = self._inner.create_pr(head=head, base=base, title=title, body=body, draft=draft)
        self._remember(pr)
        self._obs.event(_ticket_of_branch(head), f"PR #{pr.number} opened ({head} -> {base})")
        return pr

    def find_pr(self, *, head: str, base: str) -> PullRequest | None:
        return self._remember(self._inner.find_pr(head=head, base=base))

    def latest_pr(self, *, head: str, base: str) -> PullRequest | None:
        return self._remember(self._inner.latest_pr(head=head, base=base))

    def merge_pr(self, number: int, *, method: str, head_sha: str) -> bool:
        merged = self._inner.merge_pr(number, method=method, head_sha=head_sha)
        self._obs.event(self._tickets.get(number), f"PR #{number} {'merged' if merged else 'merge refused'} ({method})")
        return merged

    def _remember(self, pr: PullRequest | None) -> PullRequest | None:
        ticket = _ticket_of_branch(pr.head) if pr else None
        if pr and ticket is not None:
            self._tickets[pr.number] = ticket
        return pr


class ObservedAgents:
    def __init__(self, inner: Any, observer: "Observer") -> None:
        self._inner = inner
        self._obs = observer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def start(self, request: SessionRequest) -> SessionResult:
        match = _TICKET_DIR.match(request.cwd.name)
        ticket = int(match.group(1)) if match else None
        self._obs.event(ticket, f"{request.role} session started")
        started = time.monotonic()
        with self._obs.session(ticket, request.role):
            result = self._inner.start(request)
        took = round(time.monotonic() - started)
        outcome = "timed out" if result.timed_out else f"exit {result.returncode}"
        self._obs.event(ticket, f"{request.role} session finished ({outcome}, {took}s)")
        return result


def _ticket_of_branch(branch: str) -> int | None:
    match = _TICKET_BRANCH.search(branch)
    return int(match.group(1)) if match else None
