"""The real Forge and CommentForge: GitHub through the `gh` CLI.

Rules (DECISIONS 7, 13): reads fail closed (an error raises, it never reads as
"none"); label and comment writes retry once; a merge is never retried and is
always pinned with `--match-head-commit`; only same-repo PRs exist for the
conductor. Stateless, so safe to call from several threads.
"""
import json
import logging
import re
from typing import Any

from conductor.gh_runner import Gh, GhError, Runner
from conductor.ports import SHA, Blocker, Comment, Issue, PullRequest

log = logging.getLogger("conductor")

_METHODS = {"merge": "--merge", "squash": "--squash"}
_PR_STATE = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}
_URL_PREFIX = "https://api.github.com/repos/"


def repo_of(issue: dict[str, Any]) -> str:
    """`owner/name` (lower case) from an issue object's `repository_url`."""
    url = str(issue.get("repository_url", ""))
    return url.removeprefix(_URL_PREFIX).lower() if url.startswith(_URL_PREFIX) else ""


def to_issue(data: dict[str, Any]) -> Issue:
    labels = frozenset(str(label["name"]) for label in data.get("labels", []) if isinstance(label, dict) and "name" in label)
    return Issue(int(data["number"]), str(data.get("title", "")), labels, str(data.get("state", "open")), repo_of(data))


class GhForge:
    def __init__(self, repo: str, runner: Runner) -> None:
        self._repo = repo  # "owner/name"
        self._gh = Gh(runner)

    # --- issues ---
    def get_issue(self, number: int) -> Issue:
        data = self._gh.api(f"repos/{self._repo}/issues/{number}")
        if not isinstance(data, dict):
            raise GhError(f"issue #{number}: unexpected response")
        return to_issue(data)

    def list_sub_issues(self, prd: int) -> list[Issue]:
        return [to_issue(i) for i in self._gh.pages(f"repos/{self._repo}/issues/{prd}/sub_issues")]

    def list_blockers(self, number: int) -> list[Blocker]:
        found = self._gh.pages(f"repos/{self._repo}/issues/{number}/dependencies/blocked_by")
        return [Blocker(repo_of(b), int(b["number"]), str(b.get("state", "open"))) for b in found]

    def set_labels(self, number: int, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> None:
        if not add and not remove:
            return
        args = ["issue", "edit", str(number), "--repo", self._repo]
        for label in add:
            args += ["--add-label", label]
        for label in remove:
            args += ["--remove-label", label]
        try:
            self._gh.run_retry(*args)
        except GhError as error:  # DECISIONS 13: retry once, then log
            log.error(f"label write on #{number} failed after a retry: {error}")

    def comment(self, number: int, body: str) -> None:
        try:
            self.create_comment(number, body)
        except GhError as error:
            log.error(f"comment on #{number} failed after a retry: {error}")

    def close_issue(self, number: int) -> None:
        self._gh.run_retry("issue", "close", str(number), "--repo", self._repo)

    # --- comments (CommentForge): raise on errors ---
    def list_comments(self, number: int) -> list[Comment]:
        return [Comment(int(c["id"]), str(c.get("body") or "")) for c in self._gh.pages(f"repos/{self._repo}/issues/{number}/comments")]

    def create_comment(self, number: int, body: str) -> int:
        path = f"repos/{self._repo}/issues/{number}/comments"
        data = self._gh.api_retry(path, "-f", f"body={body}", method="POST")
        if not isinstance(data, dict) or "id" not in data:
            raise GhError(f"comment on #{number}: unexpected response")
        return int(data["id"])

    def update_comment(self, comment_id: int, body: str) -> None:
        path = f"repos/{self._repo}/issues/comments/{comment_id}"
        self._gh.api_retry(path, "-f", f"body={body}", method="PATCH")

    # --- pull requests (same-repo only) ---
    def create_pr(self, *, head: str, base: str, title: str, body: str, draft: bool = False) -> PullRequest:
        args = ["pr", "create", "--repo", self._repo, "--head", head, "--base", base, "--title", title, "--body", body]
        if draft:
            args.append("--draft")
        lines = self._gh.run(*args).strip().splitlines()
        url = lines[-1] if lines else ""
        found = re.search(r"/pull/(\d+)\s*$", url)
        if not found:
            raise GhError(f"gh pr create: no PR url in the output: {url[:200]!r}")
        return PullRequest(int(found.group(1)), head, base)

    def _prs(self, head: str, base: str, state: str) -> list[dict[str, Any]]:
        out = self._gh.run(
            "pr", "list", "--repo", self._repo, "--head", head, "--base", base, "--state", state,
            "--limit", "100", "--json", "number,headRefName,baseRefName,state,isCrossRepository",
        )
        try:
            items = json.loads(out or "[]")
        except ValueError:
            raise GhError("gh pr list: unreadable JSON") from None
        return [
            p for p in items
            if p.get("isCrossRepository") is False and p.get("headRefName") == head and p.get("baseRefName") == base
        ]

    def find_pr(self, *, head: str, base: str) -> PullRequest | None:
        prs = self._prs(head, base, "open")
        return PullRequest(int(prs[0]["number"]), head, base) if prs else None

    def latest_pr(self, *, head: str, base: str) -> PullRequest | None:
        prs = self._prs(head, base, "all")
        if not prs:
            return None
        newest = max(prs, key=lambda p: int(p["number"]))
        state = _PR_STATE.get(str(newest.get("state")), "closed")
        return PullRequest(int(newest["number"]), head, base, state)

    def pr_head_sha(self, number: int) -> str:
        sha = self._gh.run("pr", "view", str(number), "--repo", self._repo, "--json", "headRefOid", "--jq", ".headRefOid").strip()
        if not SHA.match(sha):
            raise GhError(f"PR #{number}: unexpected head sha {sha!r}")
        return sha

    def changed_files(self, number: int) -> list[str] | None:
        try:
            out = self._gh.run("pr", "diff", str(number), "--repo", self._repo, "--name-only")
        except GhError:
            return None  # unreadable: callers fail closed
        return [line for line in out.splitlines() if line]

    def mark_ready(self, number: int) -> None:
        self._gh.run_retry("pr", "ready", str(number), "--repo", self._repo)

    def merge_pr(self, number: int, *, method: str, head_sha: str) -> bool:
        """Never retried; True only when gh reports success."""
        if method not in _METHODS or not SHA.match(head_sha):
            return False
        try:
            self._gh.run("pr", "merge", str(number), "--repo", self._repo, _METHODS[method], "--match-head-commit", head_sha)
        except GhError as error:
            log.warning(f"merge of PR #{number} refused: {error}")
            return False
        return True
