---
name: ralph-gh
description: Run the ralph-gh conductor on a PRD in the current repo. It works the PRD's queued sub-issues in parallel, each in its own claude session and worktree, merges them into one integration branch, then reviews and merges one final PR, with all state on GitHub. Use when the user wants to drive a PRD's child issues to completion semi-autonomously, or asks to start / run / kick off ralph-gh. Requires .ralph-gh.toml in the repo root.
---

# /ralph-gh

This skill is a thin wrapper around the installed `ralph-gh` launcher (`${CLAUDE_PLUGIN_ROOT}/ralph-gh`, inside the installed plugin). The conductor is the real implementation — read `${CLAUDE_PLUGIN_ROOT}/README.md` for the full design.

## What to do

1. Verify you are in a git repo and `.ralph-gh.toml` exists at the repo root. If it is missing, tell the user to copy `${CLAUDE_PLUGIN_ROOT}/example.ralph-gh.toml` to `<repo>/.ralph-gh.toml` and edit it — do NOT generate one yourself. If only an old `.ralph-gh.config` exists, the conductor refuses to start and prints the old-to-new key table: show it to the user.
2. Make sure the user named a PRD: the command needs `--prd N`. If `$ARGUMENTS` has no `--prd`, ask for the PRD issue number instead of guessing.
3. Run the conductor. If `$ARGUMENTS` starts with the subcommand `run` or `stop`, forward it as is; otherwise put `run` in front of it:

```bash
"${CLAUDE_PLUGIN_ROOT}/ralph-gh" $ARGUMENTS          # $ARGUMENTS starts with run or stop
"${CLAUDE_PLUGIN_ROOT}/ralph-gh" run $ARGUMENTS      # otherwise (e.g. --prd 52)
```

4. Right after the run starts, offer the user `/ralph` to follow it (see "Following a run"). While it runs it prints progress. When it exits, show the user the contents of `last-run.md`. It lives in the per-repo state directory, **not** in the repo:

```bash
state="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/ralph-gh/state/$(gh repo view --json nameWithOwner --jq .nameWithOwner | sed 's#/#__#')"
cat "$state/last-run.md"
```

   Also tell the user the exit code (0 done or withheld by autonomy or stopped by the operator, 1 startup or config error, 2 CLI usage error, 3 the run ended incomplete, 129 killed by SIGHUP) and that the PRD carries a single `## ralph-gh status` comment, edited in place, with the phase timeline of the whole run — the place to look if something seems stalled. The final review's verdicts are `## Gate verdict` comments on the final PR.

## Following a run

The `/ralph` dashboard is a pane inside Claude Code that follows a run: the PRD's pipeline, a progress bar, the tickets in flight, the ones that need attention and the next ready ones.

- Offer the user `/ralph` right after starting a run, e.g. "Run `/ralph` to follow it."
- When the user asks how a run is going, point them to `/ralph` instead of reading `run.log` or tailing it, and do not summarize the log yourself.
- `/ralph` watches the current repo; `/ralph owner/repo` watches another one; `/ralph off` (or closing the pane) stops watching.
- The pane's **Drain** button requests a graceful stop: it writes the same `STOP` file as `ralph-gh stop`, and nothing else.
- It is on demand: nothing polls until `/ralph` is called. Loading the plugin only registers the command, and after `/ralph off` polling stops.

## Arguments

Forward whatever the user passed (a leading `run` is optional). Common forms:

- `/ralph-gh run --prd 52` — default (`--autonomy respect-hitl-arch`, `parallel` from `.ralph-gh.toml`)
- `/ralph-gh run --prd 52 --autonomy halt-each-pr` — conservative: the final PR is reviewed but never merged by the conductor
- `/ralph-gh run --prd 52 --autonomy yolo` — the final PR auto-merges when its whole diff matches `yolo_allowlist`
- `/ralph-gh run --prd 52 --parallel 1` — one ticket at a time (parallel runs multiply usage; see the README)
- `/ralph-gh run --help` — print the CLI usage (run `ralph-gh --help` directly)

## Do NOT

- Do NOT re-implement the loop logic in this session. The conductor spawns its own fresh `claude --print` sub-sessions.
- Do NOT modify `.ralph-gh.toml` unless the user explicitly asks.
- Do NOT manually edit `ralph:*` labels — the conductor owns them.

## When to push back

- If the PRD has no `ralph:queued` sub-issue, the run stops at startup. Warn the user and suggest labelling the tickets first.
- If the working tree isn't clean, the conductor refuses to start. Surface the git status to the user.

## Stopping a run

The conductor runs as a foreground process inside this session's Bash tool call, so it has **no controlling terminal** — `Ctrl-C` (SIGINT) never reaches it here. The stop request is a file in the state directory, written by `ralph-gh stop` (run it from another terminal) or by hand:

```bash
touch "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/ralph-gh/state/<owner>__<repo>/STOP"
```

The dashboard's **Drain** button does the same. This requests a **graceful** stop: in-flight tickets finish normally, nothing new is dispatched, and the run exits (`stopped by operator`, exit 0). There is no way to request an **immediate** stop (second `Ctrl-C` or `SIGTERM`) from inside this session — that requires signaling the process from a real shell, e.g. `kill -TERM <pid>`; the pid is the one in the `lock` file of the same state directory. See the README's "Stopping a run" section.
