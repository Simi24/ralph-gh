"""In-memory fakes for the Forge and Agents ports.

FakeForge keeps issues, PRs and a trail of label states in memory.
FakeAgents answers each session by role with a scripted behavior; behaviors
are plain functions `(SessionRequest) -> SessionResult`. Ready-made ones:
`commits_file(name)` (writes, commits, reports done), `says(text)`.
"""
import subprocess
from collections.abc import Callable
from dataclasses import replace

from conductor.ports import Blocker, Issue, PullRequest, SessionRequest, SessionResult

Behavior = Callable[[SessionRequest], SessionResult]


class FakeForge:
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = {i.number: i for i in issues}
        self.sub_issues: dict[int, list[int]] = {}
        self.blockers: dict[int, list[Blocker]] = {}
        self.prs: dict[int, dict[str, str]] = {}
        self.merges: list[tuple[int, str, str]] = []  # (pr, method, head_sha)
        self.label_trail: dict[int, list[frozenset[str]]] = {}

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
        issue = self.issues[number]
        labels = (issue.labels - frozenset(remove)) | frozenset(add)
        self.issues[number] = replace(issue, labels=labels)
        self.label_trail.setdefault(number, [issue.labels]).append(labels)

    def create_pr(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        number = 100 + len(self.prs) + 1
        self.prs[number] = {"head": head, "base": base, "title": title, "body": body, "state": "open"}
        return PullRequest(number, head, base)

    def merge_pr(self, number: int, *, method: str, head_sha: str) -> bool:
        self.prs[number]["state"] = "merged"
        self.merges.append((number, method, head_sha))
        return True

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
