"""Startup reconciliation of the board with reality (critical path: label transitions).

No conductor is running when this runs (the run lock is held), so a ticket
still labelled `ralph:in-progress` or `ralph:in-review` was left by an
earlier run: stopped, crashed, or changed by a human in the meantime. Each
one is settled from its ticket PR:

    merged                   -> ralph:integrated (a human merged it)
    closed without merging   -> ralph:queued (a human closed it; the work is redone)
    no PR at all             -> ralph:queued
    open                     -> ralph:in-review; `resumable` hands it to the scheduler

Fail closed: if the PR cannot be read the ticket is left exactly as it is.
"""
import logging

from conductor import labels
from conductor.config import Config
from conductor.naming import ticket_branch
from conductor.ports import Forge, Issue, PullRequest

log = logging.getLogger("conductor")

_LEFT_BY_A_RUN = frozenset({labels.IN_PROGRESS, labels.IN_REVIEW})


def reconcile_board(config: Config, forge: Forge, integration: str, tickets: list[Issue]) -> None:
    for ticket in tickets:
        if ticket.state != "open" or not (_LEFT_BY_A_RUN & ticket.labels):
            continue
        try:
            pr = forge.latest_pr(head=ticket_branch(config.branch_prefix, config.prd, ticket.number), base=integration)
            _settle(forge, ticket, pr)
        except Exception as error:  # noqa: BLE001 - unreadable means unknown: leave the ticket alone
            log.warning(f"[reconcile] ticket #{ticket.number}: left as is ({error})")


def _settle(forge: Forge, ticket: Issue, pr: PullRequest | None) -> None:
    n = ticket.number
    gone = tuple(_LEFT_BY_A_RUN & ticket.labels)
    if pr is not None and pr.state == "merged":
        forge.set_labels(n, add=(labels.INTEGRATED,), remove=gone)
        forge.comment(n, f"ralph-gh: PR #{pr.number} was merged while no conductor was running; ticket #{n} is integrated.")
    elif pr is not None and pr.state == "open":
        if labels.IN_PROGRESS in ticket.labels:
            forge.set_labels(n, add=(labels.IN_REVIEW,), remove=(labels.IN_PROGRESS,))
    else:  # closed without merging, or never opened
        forge.set_labels(n, add=(labels.QUEUED,), remove=gone)
        what = f"PR #{pr.number} was closed without merging" if pr is not None else "it has no PR"
        forge.comment(n, f"ralph-gh: {what}; ticket #{n} is back in the queue.")


def resumable(config: Config, forge: Forge, integration: str, tickets: list[Issue]) -> list[tuple[Issue, PullRequest]]:
    """Open tickets in review with an open PR: the scheduler continues them (verify, gate, merge)."""
    found: list[tuple[Issue, PullRequest]] = []
    for ticket in tickets:
        if ticket.state != "open" or labels.IN_REVIEW not in ticket.labels:
            continue
        try:
            pr = forge.find_pr(head=ticket_branch(config.branch_prefix, config.prd, ticket.number), base=integration)
        except Exception as error:  # noqa: BLE001
            log.warning(f"[reconcile] ticket #{ticket.number}: cannot resume ({error})")
            continue
        if pr is not None:
            found.append((ticket, pr))
    return sorted(found, key=lambda pair: pair[0].number)
