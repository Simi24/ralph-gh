"""The final PR: integration branch -> base branch (critical path: merges to base).

Flow: a draft PR is opened after the first ticket merge; once every ticket is
integrated one final review runs; on PASS the PR is marked ready and merged
according to the autonomy mode, pinned to the head commit the review saw.
Every doubt means no merge.
"""
import re
from collections.abc import Callable

from conductor import labels
from conductor.autonomy import decide_merge
from conductor.config import Config
from conductor.final_prompt import final_review_prompt
from conductor.markers import Verdict, verdict_of
from conductor.ports import Agents, Forge, Git, Issue, PullRequest, SessionRequest, SessionResult
from conductor.result import EXIT_INCOMPLETE, EXIT_OK, RunResult
from conductor.usage_limit import UsageLimitHit, describe
from conductor.verdict_comment import verdict_comment

_SHA = re.compile(r"^[0-9a-f]{40}$")
_CONVENTIONAL = re.compile(r"^[a-z]+(\([^)]*\))?!?: \S")


def final_pr_title(prd: Issue) -> str:
    """The final PR is squash-merged: its title is the one commit on base."""
    title = prd.title.strip()
    if _CONVENTIONAL.match(title):
        return title
    return f"feat: {title}"


def ensure_draft_pr(config: Config, forge: Forge, integration: str) -> PullRequest:
    """Open the draft final PR once; a resumed run finds and reuses it."""
    existing = forge.find_pr(head=integration, base=config.base_branch)
    if existing is not None:
        return existing
    prd = forge.get_issue(config.prd)
    closes = [config.prd, *(t.number for t in forge.list_sub_issues(config.prd))]
    body = f"Final PR of PRD #{config.prd}.\n\n" + "\n".join(f"Closes #{n}" for n in closes) + "\n"
    return forge.create_pr(
        head=integration, base=config.base_branch, title=final_pr_title(prd), body=body, draft=True
    )


def open_draft_after_first_merge(config: Config, forge: Forge, integration: str) -> None:
    """Best effort: the final stage opens the PR itself if this fails."""
    try:
        ensure_draft_pr(config, forge, integration)
    except Exception:  # noqa: BLE001 - never let this fail an already merged ticket
        pass


def _review(
    config: Config, agents: Agents, git: Git, pr: PullRequest, integration: str, head_sha: str
) -> tuple[SessionResult, Verdict]:
    """Run the final review, once more with a fresh session if the verdict is unparsable."""
    worktree = config.state_root / f"prd-{config.prd}" / "worktrees" / "final-review"
    branch = f"{integration}-final-review"
    git.add_worktree(worktree, branch, integration)
    try:
        for _attempt in range(2):
            result = agents.start(
                SessionRequest(
                    role="final-review",
                    prompt=final_review_prompt(config, pr.number, integration, head_sha),
                    cwd=worktree,
                    agent=config.reviewer_agent,
                    timeout=config.session_timeout,
                )
            )
            verdict = verdict_of(result)
            if verdict is not Verdict.UNPARSABLE or result.usage_limit:
                break
        return result, verdict
    finally:
        git.remove_worktree(worktree, branch)


def _hitl_flagged(config: Config, forge: Forge) -> bool | None:
    try:
        issues = [forge.get_issue(config.prd), *forge.list_sub_issues(config.prd)]
    except Exception:  # noqa: BLE001 - unreadable means unknown, and unknown withholds
        return None
    return any(labels.HITL_ARCH in i.labels for i in issues)


def _block_prd(config: Config, forge: Forge, pr: PullRequest, note: str) -> None:
    forge.set_labels(config.prd, add=(labels.BLOCKED,))
    forge.comment(pr.number, note)


def run_final(config: Config, forge: Forge, agents: Agents, git: Git, integration: str) -> RunResult:
    tickets = forge.list_sub_issues(config.prd)
    if not tickets or any(labels.INTEGRATED not in t.labels for t in tickets):
        return RunResult(EXIT_INCOMPLETE, "final review not started: not every ticket is integrated")

    pr = ensure_draft_pr(config, forge, integration)
    head_sha = forge.pr_head_sha(pr.number)
    if not _SHA.match(head_sha):
        return RunResult(EXIT_INCOMPLETE, "final review not started: reviewed head commit unknown")

    result, verdict = _review(config, agents, git, pr, integration, head_sha)
    if result.usage_limit:  # a pause, not a verdict: run() waits or exits (#62)
        raise UsageLimitHit(describe(result))
    if verdict is Verdict.UNPARSABLE:
        _block_prd(
            config, forge, pr,
            "Final review: no parsable verdict even after a retry -- the PR was never actually judged. "
            "Left open (draft) for a human.",
        )
        return RunResult(EXIT_INCOMPLETE, "final review unparsable twice")

    forge.comment(pr.number, verdict_comment(result.text, f"final review of {head_sha[:7]}"))
    if verdict is Verdict.FAIL:
        _block_prd(
            config, forge, pr,
            "Final review: **FAIL**. Left open (draft) for a human. See the `## Gate verdict` comment.",
        )
        return RunResult(EXIT_INCOMPLETE, "final review failed")

    if forge.pr_head_sha(pr.number) != head_sha:
        return RunResult(EXIT_INCOMPLETE, "head moved after review")

    forge.mark_ready(pr.number)
    changed = forge.changed_files(pr.number) if config.autonomy == "yolo" else None
    decision = decide_merge(config.autonomy, _hitl_flagged(config, forge), config.yolo_allowlist, changed)
    if not decision.allowed:
        forge.set_labels(config.prd, add=(labels.GATE_PASSED,))
        forge.comment(
            pr.number,
            f"Final review: **PASS** -- merge withheld by the conductor ({decision.reason}). A human decides.",
        )
        return RunResult(EXIT_OK, f"final review passed, merge withheld: {decision.reason}")

    if not forge.merge_pr(pr.number, method="squash", head_sha=head_sha):
        return RunResult(EXIT_INCOMPLETE, "final merge refused, PR left open")
    return _close_out(config, forge, tickets)


def _retry_once(action: Callable[[], None]) -> bool:
    for _ in range(2):
        try:
            action()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _tidy_ticket(forge: Forge, ticket: Issue) -> bool:
    return _retry_once(
        lambda: forge.set_labels(ticket.number, add=(labels.DONE,), remove=(labels.INTEGRATED,))
    ) and _retry_once(lambda: forge.close_issue(ticket.number))


def _close_out(config: Config, forge: Forge, tickets: list[Issue]) -> RunResult:
    """After the merge: closing keywords do nothing off the default branch, so close explicitly."""
    failed = [t.number for t in tickets if not _tidy_ticket(forge, t)]
    prd_ok = _retry_once(
        lambda: forge.set_labels(config.prd, add=(labels.DONE,), remove=(labels.GATE_PASSED,))
    ) and _retry_once(lambda: forge.close_issue(config.prd))
    if not prd_ok:
        failed.append(config.prd)
    suffix = f" (board cleanup incomplete for {', '.join(f'#{n}' for n in failed)})" if failed else ""
    return RunResult(EXIT_OK, f"final PR merged{suffix}")
