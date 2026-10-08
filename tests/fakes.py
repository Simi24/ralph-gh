"""In-memory fakes for the Forge and Agents ports.

FakeForge keeps issues, PRs and a trail of label states in memory.
FakeAgents answers each session by role with a scripted behavior; behaviors
are plain functions `(SessionRequest) -> SessionResult`. Ready-made ones:
`commits_file(name)` (writes, commits, reports done), `says(text)`.
"""
import subprocess
import threading
from collections.abc import Callable
from dataclasses import replace

from conductor.ports import Blocker, Comment, Issue, PullRequest, SessionRequest, SessionResult

Behavior = Callable[[SessionRequest], SessionResult]


class FakeForge:
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = {i.number: i for i in issues}
        self.sub_issues: dict[int, list[int]] = {}
        self.blockers: dict[int, list[Blocker]] = {}
        self.prs: dict[int, dict[str, str]] = {}
        self.merges: list[tuple[int, str, str]] = []  # (pr, method, head_sha)
        self.label_trail: dict[int, list[frozenset[str]]] = {}
        self.comments: dict[int, list[tuple[int, str]]] = {}  # number -> [(comment id, body)]
        self.comment_seq = 0
        self.refuse_merge: dict[str, int] = {}  # head branch -> merges still to refuse (a conflict)
        self.write_threads: set[int] = set()  # ids of every thread that wrote to the forge
        self.writes = 0
        # Final-PR support: head moves are scripted through `pr_heads`.
        self.pr_heads: dict[int, str] = {}
        self.final_diff: list[str] | None = ["README.md"]  # None = unreadable
        self.ready: list[int] = []
        self.closed: list[int] = []

    def _write(self) -> None:
        self.writes += 1
        self.write_threads.add(threading.get_ident())

    def add_sub_issues(self, prd: int, tickets: list[Issue]) -> None:
        for t in tickets:
            self.issues[t.number] = t
        self.sub_issues[prd] = [t.number for t in tickets]

    def get_issue(self, number: int) -> Issue:
        return self.issues[number]

    def list_sub_issues(self, prd: int) -> list[Issue]:
        return [self.issues[n] for n in self.sub_issues.get(prd, [])]

    def list_blockers(self, number: int) -> list[Blocker]:
        return list(self.blockers.get(number, []))

    def set_labels(self, number: int, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> None:
        self._write()
        issue = self.issues[number]
        labels = (issue.labels - frozenset(remove)) | frozenset(add)
        self.issues[number] = replace(issue, labels=labels)
        self.label_trail.setdefault(number, [issue.labels]).append(labels)

    def comment(self, number: int, body: str) -> None:
        self.create_comment(number, body)

    def create_pr(self, *, head: str, base: str, title: str, body: str, draft: bool = False) -> PullRequest:
        self._write()
        number = 100 + len(self.prs) + 1
        self.prs[number] = {
            "head": head, "base": base, "title": title, "body": body, "state": "open", "draft": draft,
        }
        return PullRequest(number, head, base)

    def find_pr(self, *, head: str, base: str) -> PullRequest | None:
        for number, pr in self.prs.items():
            if (pr["head"], pr["base"], pr["state"]) == (head, base, "open"):
                return PullRequest(number, head, base)
        return None

    def latest_pr(self, *, head: str, base: str) -> PullRequest | None:
        found = [n for n, pr in self.prs.items() if (pr["head"], pr["base"]) == (head, base)]
        if not found:
            return None
        number = max(found)
        return PullRequest(number, head, base, str(self.prs[number]["state"]))

    def pr_head_sha(self, number: int) -> str:
        return self.pr_heads.setdefault(number, f"{number:040x}")

    def changed_files(self, number: int) -> list[str] | None:
        return self.final_diff

    def mark_ready(self, number: int) -> None:
        self._write()
        self.prs[number]["draft"] = False
        self.ready.append(number)

    def close_issue(self, number: int) -> None:
        self._write()
        self.issues[number] = replace(self.issues[number], state="closed")
        self.closed.append(number)

    def merge_pr(self, number: int, *, method: str, head_sha: str) -> bool:
        self._write()
        head = self.prs[number]["head"]
        if self.refuse_merge.get(head, 0) > 0:
            self.refuse_merge[head] -= 1
            return False
        # A pin that no longer matches the head is refused, like GitHub does.
        if number in self.pr_heads and self.pr_heads[number] != head_sha:
            return False
        self.prs[number]["state"] = "merged"
        self.merges.append((number, method, head_sha))
        return True

    # --- comments (#64) ---
    def list_comments(self, number: int) -> list[Comment]:
        return [Comment(i, b) for i, b in self.comments.get(number, [])]

    def create_comment(self, number: int, body: str) -> int:
        self._write()
        self.comment_seq += 1
        self.comments.setdefault(number, []).append((self.comment_seq, body))
        return self.comment_seq

    def update_comment(self, comment_id: int, body: str) -> None:
        self._write()
        for items in self.comments.values():
            for index, (i, _) in enumerate(items):
                if i == comment_id:
                    items[index] = (i, body)

    def ralph_trail(self, number: int) -> list[list[str]]:
        """Distinct ralph:* label sets the issue went through, in order."""
        trail: list[list[str]] = []
        for labels in self.label_trail.get(number, []):
            ralph = sorted(label for label in labels if label.startswith("ralph:"))
            if not trail or trail[-1] != ralph:
                trail.append(ralph)
        return trail


class FakeAgents:
    def __init__(self, behaviors: dict[str, Behavior]) -> None:
        self.behaviors = behaviors
        self.requests: list[SessionRequest] = []

    def start(self, request: SessionRequest) -> SessionResult:
        self.requests.append(request)
        return self.behaviors[request.role](request)


def says(text: str) -> Behavior:
    return lambda request: SessionResult(text=text)


def commits_file(name: str, text: str = "built it\nRALPH:DONE") -> Behavior:
    def behavior(request: SessionRequest) -> SessionResult:
        (request.cwd / name).write_text("done\n")
        for args in (["add", "-A"], ["commit", "-m", f"feat: add {name}"]):
            subprocess.run(["git", *args], cwd=request.cwd, check=True, capture_output=True)
        return SessionResult(text=text)

    return behavior
