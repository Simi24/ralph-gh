---
name: ralph-gh
description: Launch the ralph-gh GitHub-issue-driven Ralph loop in the current repo. Spawns fresh claude --print sessions in series, one issue per iteration, with state on GitHub. Use when the user wants to drive a PRD's child issues to completion semi-autonomously, or asks to start / run / kick off ralph-gh. Requires .ralph-gh.config in the repo root.
---

# /ralph-gh

This skill is a thin wrapper around `~/.claude/ralph-gh/ralph-gh.sh`. The script is the real implementation — read `~/.claude/ralph-gh/README.md` and `~/.claude/ralph-gh/CLAUDE.md` for the full design.

## What to do

1. Verify you are in a git repo and `.ralph-gh.config` exists at the repo root. If missing, tell the user to copy `~/.claude/ralph-gh/example.ralph-gh.config` to `<repo>/.ralph-gh.config` and edit it — do NOT generate one yourself.
2. Run the orchestrator, forwarding `$ARGUMENTS` verbatim:

```bash
~/.claude/ralph-gh/ralph-gh.sh $ARGUMENTS
```

3. While the script runs, it will print iteration progress, including `[gate]` lines: after each iteration the orchestrator runs a binding EXTERNAL review gate on the open PR and merges only on `GATE:PASS` (fix sessions + re-gate on FAIL). When it exits, show the user the contents of `.ralph-gh/last-run.md` and mention that gate verdicts live as `## Gate verdict` comments on the PRs.

## Arguments

Forward whatever the user passed. Common forms:

- `/ralph-gh` — default (`--autonomy=respect-hitl-arch --max-iterations=20`)
- `/ralph-gh --autonomy=halt-each-pr --max-iterations=1` — conservative single PR
- `/ralph-gh --autonomy=yolo --max-iterations=10` — full-auto with yolo allowlist
- `/ralph-gh --help` — print the script's usage

## Do NOT

- Do NOT re-implement the loop logic in this session. The script spawns its own fresh `claude --print` sub-sessions for each iteration.
- Do NOT modify `.ralph-gh.config` unless the user explicitly asks.
- Do NOT manually call `gh issue edit` to manage `ralph:*` labels — the iteration sessions and the orchestrator (label watcher + external gate) manage them.

## When to push back

- If the user runs `/ralph-gh` and no issue has `ralph:queued`, the first iteration will emit `QUEUE_EMPTY` immediately. Warn them and suggest labeling issues first.
- If the working tree isn't clean, the script refuses to start. Surface the git status to the user.
