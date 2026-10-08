"""GhForge against a scripted `gh`: the commands it builds and how it reads the answers."""
import json
import subprocess
import unittest

from conductor.gh_forge import GhForge
from conductor.gh_runner import GhError
from conductor.ports import Blocker, Comment, Issue, PullRequest

SHA = "a" * 40
API = "https://api.github.com/repos/Me/Repo"


def issue_json(number: int, state: str = "open", labels: tuple[str, ...] = (), repo: str = API) -> dict[str, object]:
    return {"number": number, "title": f"t{number}", "state": state, "repository_url": repo, "labels": [{"name": n} for n in labels]}


class Script:
    """A Runner: answers by the first matching rule; records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.rules: list[tuple[str, list[subprocess.CompletedProcess[str]]]] = []

    def on(self, needle: str, *answers: str | int) -> None:
        done = [
            subprocess.CompletedProcess([], 1 if isinstance(a, int) else 0, "" if isinstance(a, int) else a, "denied")
            for a in answers
        ]
        self.rules.append((needle, done))

    def __call__(self, args: list[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(args)
        line = " ".join(args)
        for needle, answers in self.rules:
            if needle in line:
                return answers.pop(0) if len(answers) > 1 else answers[0]
        return subprocess.CompletedProcess([], 1, "", f"no rule for {line}")


class GhForgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gh = Script()
        self.forge = GhForge("Me/Repo", self.gh)

    def last(self) -> str:
        return " ".join(self.gh.calls[-1])

    def test_get_issue_reads_labels_state_and_repo(self) -> None:
        self.gh.on("issues/5", json.dumps(issue_json(5, "closed", ("ralph:queued",))))
        self.assertEqual(self.forge.get_issue(5), Issue(5, "t5", frozenset({"ralph:queued"}), "closed", "me/repo"))

    def test_sub_issues_are_paginated_with_per_page_100(self) -> None:
        page1 = json.dumps([issue_json(n) for n in range(100)])
        page2 = json.dumps([issue_json(100)])
        self.gh.on("&page=1", page1)
        self.gh.on("&page=2", page2)
        found = self.forge.list_sub_issues(52)
        self.assertEqual(len(found), 101)
        self.assertTrue(all("per_page=100" in " ".join(c) for c in self.gh.calls))
        self.assertIn("repos/Me/Repo/issues/52/sub_issues", self.last())

    def test_blockers_are_keyed_by_repo_and_number(self) -> None:
        other = "https://api.github.com/repos/Other/Lib"
        self.gh.on("blocked_by", json.dumps([issue_json(3), issue_json(9, "closed", repo=other)]))
        self.assertEqual(self.forge.list_blockers(4), [Blocker("me/repo", 3, "open"), Blocker("other/lib", 9, "closed")])

    def test_reads_fail_closed(self) -> None:
        self.gh.on("blocked_by", 1)
        with self.assertRaises(GhError):
            self.forge.list_blockers(4)
        self.gh.on("sub_issues", "{not json")
        with self.assertRaises(GhError):
            self.forge.list_sub_issues(52)

    def test_set_labels_retries_once_then_logs(self) -> None:
        self.gh.on("issue edit", 1, "")
        self.forge.set_labels(7, add=("a", "b"), remove=("c",))
        self.assertEqual(len(self.gh.calls), 2)
        self.assertEqual(
            self.gh.calls[-1], ["issue", "edit", "7", "--repo", "Me/Repo", "--add-label", "a", "--add-label", "b", "--remove-label", "c"]
        )
        failing = Script()
        failing.on("issue edit", 1)
        with self.assertLogs("conductor", "ERROR"):
            GhForge("Me/Repo", failing).set_labels(7, add=("a",))
        self.assertEqual(len(failing.calls), 2)

    def test_comments(self) -> None:
        self.gh.on("-X GET repos/Me/Repo/issues/8/comments", json.dumps([{"id": 1, "body": "x"}, {"id": 2, "body": None}]))
        self.assertEqual(self.forge.list_comments(8), [Comment(1, "x"), Comment(2, "")])
        self.gh.on("-X POST repos/Me/Repo/issues/8/comments", json.dumps({"id": 42}))
        self.assertEqual(self.forge.create_comment(8, "@body"), 42)
        self.assertIn("body=@body", self.gh.calls[-1])  # -f: literal text, never a file
        self.gh.on("-X PATCH repos/Me/Repo/issues/comments/42", "{}")
        self.forge.update_comment(42, "new")
        self.assertIn("body=new", self.gh.calls[-1])

    def test_comment_write_raises_for_create_but_only_logs_for_comment(self) -> None:
        self.gh.on("-X POST", 1)
        with self.assertRaises(GhError):
            self.forge.create_comment(8, "x")
        with self.assertLogs("conductor", "ERROR"):
            self.forge.comment(8, "x")

    def test_create_pr_parses_the_number_and_honours_draft(self) -> None:
        self.gh.on("pr create", "https://github.com/Me/Repo/pull/77\n")
        pr = self.forge.create_pr(head="h", base="b", title="T", body="B", draft=True)
        self.assertEqual(pr, PullRequest(77, "h", "b"))
        self.assertEqual(
            self.gh.calls[-1],
            ["pr", "create", "--repo", "Me/Repo", "--head", "h", "--base", "b", "--title", "T", "--body", "B", "--draft"],
        )
        self.forge.create_pr(head="h", base="b", title="T", body="B")
        self.assertNotIn("--draft", self.gh.calls[-1])

    def test_create_pr_without_a_url_raises(self) -> None:
        self.gh.on("pr create", "nothing useful")
        with self.assertRaises(GhError):
            self.forge.create_pr(head="h", base="b", title="T", body="B")

    def test_find_and_latest_pr_ignore_forks_and_other_branches(self) -> None:
        prs = [
            {"number": 5, "headRefName": "h", "baseRefName": "b", "state": "MERGED", "isCrossRepository": False},
            {"number": 9, "headRefName": "h", "baseRefName": "b", "state": "OPEN", "isCrossRepository": True},
            {"number": 7, "headRefName": "h", "baseRefName": "b", "state": "CLOSED", "isCrossRepository": False},
            {"number": 8, "headRefName": "other", "baseRefName": "b", "state": "OPEN", "isCrossRepository": False},
        ]
        self.gh.on("--state all", json.dumps(prs))
        self.gh.on("--state open", json.dumps([prs[1], prs[3]]))
        self.assertEqual(self.forge.latest_pr(head="h", base="b"), PullRequest(7, "h", "b", "closed"))
        self.assertIsNone(self.forge.find_pr(head="h", base="b"))
        again = Script()
        again.on("--state open", json.dumps([{**prs[0], "state": "OPEN"}]))
        self.assertEqual(GhForge("Me/Repo", again).find_pr(head="h", base="b"), PullRequest(5, "h", "b"))

    def test_pr_head_sha_must_be_a_sha(self) -> None:
        self.gh.on("pr view 3", SHA + "\n")
        self.assertEqual(self.forge.pr_head_sha(3), SHA)
        self.gh.on("pr view 4", "not-a-sha")
        with self.assertRaises(GhError):
            self.forge.pr_head_sha(4)

    def test_changed_files_is_none_when_unreadable(self) -> None:
        self.gh.on("pr diff 3", "a.py\nb/c.md\n")
        self.assertEqual(self.forge.changed_files(3), ["a.py", "b/c.md"])
        self.gh.on("pr diff 4", 1)
        self.assertIsNone(self.forge.changed_files(4))

    def test_merge_is_pinned_to_the_head_and_never_retried(self) -> None:
        self.gh.on("pr merge", "")
        self.assertTrue(self.forge.merge_pr(3, method="merge", head_sha=SHA))
        self.assertEqual(self.gh.calls[-1], ["pr", "merge", "3", "--repo", "Me/Repo", "--merge", "--match-head-commit", SHA])
        self.assertTrue(self.forge.merge_pr(3, method="squash", head_sha=SHA))
        self.assertIn("--squash", self.gh.calls[-1])
        merges = len(self.gh.calls)
        self.assertFalse(self.forge.merge_pr(3, method="rebase", head_sha=SHA))
        self.assertFalse(self.forge.merge_pr(3, method="merge", head_sha="abc"))
        self.assertEqual(len(self.gh.calls), merges)  # refused before gh was called

    def test_a_refused_merge_is_false_and_called_once(self) -> None:
        self.gh.on("pr merge", 1)
        self.assertFalse(self.forge.merge_pr(3, method="merge", head_sha=SHA))
        self.assertEqual(len(self.gh.calls), 1)

    def test_mark_ready_and_close(self) -> None:
        self.gh.on("pr ready", "")
        self.gh.on("issue close", "")
        self.forge.mark_ready(3)
        self.forge.close_issue(4)
        self.assertEqual([c[:3] for c in self.gh.calls], [["pr", "ready", "3"], ["issue", "close", "4"]])


if __name__ == "__main__":
    unittest.main()
