"""The final PR: integration branch -> base branch (critical path: merges to base).

Flow: a draft PR is opened after the first ticket merge; once every ticket is
integrated one final review runs; on PASS the PR is marked ready and merged
according to the autonomy mode, pinned to the head commit the review saw.
On FAIL one fix session per round gets the BLOCKING findings (bounded by
`gate_fix_rounds`) and the next review is scoped to the fix diff when that is
provably safe (gate_scope.py). Every doubt means no merge.
"""
import re
import time
from pathlib import Path

from conductor import labels
from conductor.attempt import attempt
from conductor.autonomy import decide_merge
from conductor.config import Config
from conductor.final_fix import FixStatus, run_fix_round
from conductor.final_prompt import Rescope, final_review_prompt
from conductor.findings import blocking_findings
from conductor.gate_scope import Scope, resolve_gate_scope
from conductor.head_wait import wait_for_head
from conductor.markers import Verdict, verdict_of
from conductor.observer import Observer
from conductor.ports import SHA, Agents, Forge, Git, Issue, PullRequest, SessionRequest, SessionResult
from conductor.result import EXIT_INCOMPLETE, EXIT_OK, RunResult
from conductor.state_dir import prd_dir
from conductor.usage_limit import UNKNOWN_RESET, Sleep, UsageLimitHit, describe
from conductor.verdict_comment import verdict_comment

_CONVENTIONAL = re.compile(r"^[a-z]+(\([^)]*\))?!?: \S")


def final_pr_title(prd: Issue) -> str:
    """The final PR is squash-merged: its title is the one commit on base."""
    title = prd.title.strip()
    if _CONVENTIONAL.match(title):
        return title
    return f"feat: {title}"


def numbers(issues: list[int]) -> str:
    return ", ".join(f"#{n}" for n in sorted(issues))


def not_started_reason(tickets: list[Issue]) -> str | None:
    """Why the final review cannot start, or None when it can. A CLOSED sub-issue (a human's
    wontfix) is ignored; an OPEN one that is not integrated blocks, even if it never opted in
    (no ralph label). `run._finish` uses the same rule, so it never promises a review that refuses."""
    open_tickets = [t for t in tickets if t.state == "open"]
    if not open_tickets:
        return "final review not started: no open ticket in the PRD"
    pending = [t.number for t in open_tickets if labels.INTEGRATED not in t.labels]
    if pending:
        return f"final review not started: tickets {numbers(pending)} are not integrated"
    return None


def ensure_draft_pr(config: Config, forge: Forge, integration: str) -> PullRequest:
    """Open the draft final PR once; a resumed run finds and reuses it."""
    existing = forge.find_pr(head=integration, base=config.base_branch)
    if existing is not None:
        return existing
    prd = forge.get_issue(config.prd)
    closes = [config.prd, *(t.number for t in forge.list_sub_issues(config.prd) if t.state == "open")]
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
    config: Config, agents: Agents, git: Git, pr: PullRequest, integration: str, head_sha: str,
    rescope: Rescope | None = None,
) -> tuple[SessionResult, Verdict]:
    """Run the final review, once more with a fresh session if the verdict is unparsable."""
    worktree = prd_dir(config.state_root, config.prd) / "worktrees" / "final-review"
    branch = f"{integration}-final-review"
    git.add_worktree(worktree, branch, integration)
    try:
        for _attempt in range(2):
            result = agents.start(
                SessionRequest(
                    role="final-review",
                    prompt=final_review_prompt(config, pr.number, integration, head_sha, rescope),
                    cwd=worktree,
                    agent=config.reviewer_agent,
                    timeout=config.session_timeout,
                    add_dirs=(rescope.prev_verdict.parent,) if rescope else (),
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


def _save_verdict(config: Config, round_no: int, text: str) -> Path | None:
    """The conductor's own copy of a verdict, for the next round's re-review (None if unwritable)."""
    path = prd_dir(config.state_root, config.prd) / "final" / f"verdict-round-{round_no}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError:
        return None
    return path


def run_final(
    config: Config, forge: Forge, agents: Agents, git: Git, integration: str,
    notes: Path | None = None, obs: Observer | None = None, sleep: Sleep = time.sleep,
) -> RunResult:
    tickets = forge.list_sub_issues(config.prd)
    not_started = not_started_reason(tickets)
    if not_started is not None:
        return RunResult(EXIT_INCOMPLETE, not_started)
    tickets = [t for t in tickets if t.state == "open"]

    pr = ensure_draft_pr(config, forge, integration)
    fixes = 0
    prev_sha = ""
    prev_verdict: Path | None = None
    pushed_sha = ""  # the commit the last fix round pushed: the only head the next review may see
    while True:
        round_no = fixes + 1
        if pushed_sha:  # GitHub may still report the old head right after the push: wait for ours
            if not wait_for_head(forge, pr.number, pushed_sha, sleep):
                return RunResult(
                    EXIT_INCOMPLETE,
                    f"final review not started: the PR head never reported the fix commit {pushed_sha[:7]}",
                )
            head_sha = pushed_sha
        else:
            head_sha = forge.pr_head_sha(pr.number)
        if not SHA.match(head_sha):
            return RunResult(EXIT_INCOMPLETE, "final review not started: reviewed head commit unknown")
        scope = resolve_gate_scope(
            git, round_no=round_no, prev_sha=prev_sha, head_sha=head_sha, prev_verdict=prev_verdict
        )
        rescope = Rescope(round_no, prev_sha, prev_verdict) if scope is Scope.FIX_DIFF and prev_verdict else None
        result, verdict = _review(config, agents, git, pr, integration, head_sha, rescope)
        if result.usage_limit:  # a pause, not a verdict: run() waits or exits (#62)
            raise UsageLimitHit(describe(result))
        if verdict is Verdict.UNPARSABLE:
            _block_prd(
                config, forge, pr,
                "Final review: no parsable verdict even after a retry -- the PR was never actually judged. "
                "Left open (draft) for a human.",
            )
            return RunResult(EXIT_INCOMPLETE, "final review unparsable twice")

        label = f"final review of {head_sha[:7]}, round {round_no}, {scope.value}"
        forge.comment(pr.number, verdict_comment(result.text, label))
        if verdict is not Verdict.FAIL:
            break

        # FAIL: one fix session per round, given the BLOCKING findings only.
        if fixes >= config.gate_fix_rounds:
            findings, _ = blocking_findings(result.text)
            _block_prd(
                config, forge, pr,
                f"Final review: **FAIL** after {fixes} fix round(s). Left open (draft) for a human. "
                f"Open BLOCKING findings of the last review:\n\n{findings}",
            )
            return RunResult(EXIT_INCOMPLETE, f"final review failed after {fixes} fix round(s)")
        fixes += 1
        fix = run_fix_round(config, agents, git, integration, notes, result.text, fixes, obs)
        if fix.status is FixStatus.USAGE_LIMIT:
            raise UsageLimitHit(fix.detail or UNKNOWN_RESET)  # #62
        if fix.status is not FixStatus.FIXED:
            _block_prd(
                config, forge, pr,
                f"Final review: **FAIL**; fix round {fixes} {fix.status.value}: {fix.detail}\n"
                "Left open (draft) for a human. See the `## Gate verdict` comment.",
            )
            return RunResult(EXIT_INCOMPLETE, f"final fix round {fixes} {fix.status.value}")
        prev_sha = head_sha
        pushed_sha = fix.pushed_sha
        prev_verdict = _save_verdict(config, round_no, result.text)

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


def _tidy_ticket(forge: Forge, ticket: Issue) -> bool:
    return attempt(
        lambda: forge.set_labels(ticket.number, add=(labels.DONE,), remove=(labels.INTEGRATED,))
    ) and attempt(lambda: forge.close_issue(ticket.number))


def _close_out(config: Config, forge: Forge, tickets: list[Issue]) -> RunResult:
    """After the merge: closing keywords do nothing off the default branch, so close explicitly."""
    failed = [t.number for t in tickets if not _tidy_ticket(forge, t)]
    prd_ok = attempt(
        lambda: forge.set_labels(config.prd, add=(labels.DONE,), remove=(labels.GATE_PASSED,))
    ) and attempt(lambda: forge.close_issue(config.prd))
    if not prd_ok:
        failed.append(config.prd)
    suffix = f" (board cleanup incomplete for {', '.join(f'#{n}' for n in failed)})" if failed else ""
    return RunResult(EXIT_OK, f"final PR merged{suffix}")
