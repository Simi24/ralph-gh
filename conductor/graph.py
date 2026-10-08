"""The task graph: pure logic over the PRD's tickets and their blockers.

Edges come only from native `blocked_by` dependencies (never from issue text).
A blocker is keyed by (repo, number). Inside the PRD it is satisfied once its
ticket is integrated (or already closed); outside the PRD, once it is closed.
"""
from conductor import labels
from conductor.ports import Blocker, Issue

Key = tuple[str, int]


def key_of(issue: Issue) -> Key:
    return (issue.repo, issue.number)


def find_cycle(tickets: list[Issue], blockers: dict[int, list[Blocker]]) -> list[int]:
    """Ticket numbers of one dependency cycle among the tickets (empty if none)."""
    by_key = {key_of(t): t for t in tickets}
    edges = {
        t.number: [b.number for b in blockers.get(t.number, []) if (b.repo, b.number) in by_key]
        for t in tickets
    }
    done: set[int] = set()

    def visit(node: int, path: list[int]) -> list[int]:
        if node in path:
            return path[path.index(node):]
        if node in done:
            return []
        for nxt in sorted(edges[node]):
            cycle = visit(nxt, path + [node])
            if cycle:
                return cycle
        done.add(node)
        return []

    for t in sorted(edges):
        cycle = visit(t, [])
        if cycle:
            return cycle
    return []


def _satisfied(blocker: Blocker, tickets: dict[Key, Issue]) -> bool:
    ticket = tickets.get((blocker.repo, blocker.number))
    if ticket is None:
        return blocker.state == "closed"
    return labels.INTEGRATED in ticket.labels or ticket.state == "closed"


def dispatchable(tickets: list[Issue], blockers: dict[int, list[Blocker]]) -> list[Issue]:
    """Queued tickets whose blockers are all satisfied, in pick order.

    Pick order: hitl-arch tickets last, then ascending number. Tickets that are
    not queued (or already in a ralph state) are never returned.
    """
    by_key = {key_of(t): t for t in tickets}
    ready = [
        t
        for t in tickets
        if labels.QUEUED in t.labels
        and t.state == "open"
        and all(_satisfied(b, by_key) for b in blockers.get(t.number, []))
    ]
    return sorted(ready, key=lambda t: (labels.HITL_ARCH in t.labels, t.number))
