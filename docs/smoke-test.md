# Smoke test: a two-ticket PRD on a scratch repository

This is the end-to-end check of the Python conductor against the real GitHub and the real `claude` CLI. It is **not** run by the unit tests and **not** run automatically: it creates a GitHub repository and spends real Claude sessions (a few ordinary sessions plus two reviewers), so a human approves and runs it.

Status: executed once, see the result log at the bottom. Record each further run there.

## Prerequisites

- `gh` authenticated with the `repo` and `delete_repo` scopes (`gh auth refresh -s delete_repo`), `git`, `claude` on `PATH`, Python 3.12+.
- The ralph-gh plugin installed (step 0), at a Claude Code version that meets the README's minimum. The reviewer agents come with the plugin's worker bundle: do not copy them anywhere.
- Native sub-issues and dependencies enabled (they are on by default on github.com).

## 0. Install the plugin

At the Claude Code prompt (to test an unreleased change, add the checkout first with `/plugin marketplace add /path/to/ralph-gh` and install from it):

```
/plugin install ralph-gh --marketplace Simi24/ralph-gh
```

Then set the variables used below (adjust the owner) and put the terminal launcher in place as the README's "Run it from a terminal" describes:

```
export SCRATCH=<your-gh-user>/ralph-smoke # the scratch repo
export BUNDLE="$(ls -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/ralph-gh/ralph-gh/*/worker-bundle | sort -V | tail -n1)"
```

## 1. Create the scratch repository

```
gh repo create "$SCRATCH" --private --add-readme --clone
cd "$(basename "$SCRATCH")"
```

Add a verify command and one tiny test target so the conductor has something to run. Create `.ralph-gh.toml`:

```
verify_commands = ["python3 -m unittest discover -s tests -t ."]
gate_fix_rounds = 1
parallel = 2
```

Create `tests/__init__.py` (empty) and `tests/test_placeholder.py` with one trivial passing test (`import unittest`, a `TestCase` with `def test_placeholder(self): pass`). `unittest discover` exits 5 when no test runs, so without the placeholder verify fails on the first ticket.

Commit everything to `main`, then push (`git add -A`, `git commit -m "chore: scaffold"`, `git push`). Leave `.ralph-gh.toml` committed so the tree stays clean. If a hook or branch protection blocks direct pushes to `main`, push the scaffold commit yourself.

## 2. Create the PRD and two tickets

```
gh issue create --title "PRD: greeting module" --body "Add a greeting module with a function greet(name) returning 'Hello, <name>!', and a CLI entry that prints it."
gh issue create --title "greet function" --body "## Acceptance criteria
- greet.py has greet(name) returning 'Hello, <name>!'
- tests/test_greet.py covers it"
gh issue create --title "greet CLI" --body "## Acceptance criteria
- python3 -m greet NAME prints the greeting
- tests cover the CLI output"
```

Note the three issue numbers (PRD = P, tickets = A and B). Then link them with the REST API (these endpoints take database ids, not issue numbers):

```
PRD_ID=$(gh api repos/$SCRATCH/issues/P --jq .id)
for n in A B; do
  ID=$(gh api repos/$SCRATCH/issues/$n --jq .id)
  gh api -X POST repos/$SCRATCH/issues/P/sub_issues -F sub_issue_id=$ID
done
A_ID=$(gh api repos/$SCRATCH/issues/A --jq .id)
gh api -X POST repos/$SCRATCH/issues/B/dependencies/blocked_by -F issue_id=$A_ID
```

(B is blocked by A, so the run has to go A then B.) Label both tickets `ralph:queued` (`gh issue edit A --add-label ralph:queued`; the preflight creates the label if it is missing, so run step 3 once with `--prd P` first if the label does not exist yet, then label and rerun).

## 3. Run

```
ralph-gh run --prd P --autonomy halt-each-pr
```

Run it from anywhere inside the scratch checkout (or start it with `/ralph-gh run --prd P` in Claude Code). While it runs, open `/ralph` in a Claude Code session in the scratch checkout to watch it. Expected, in order:

1. Preflight passes (tools, auth, permissions, clean tree, worker bundle loadable, both agents found, labels created).
2. Exploration notes are written under `~/.claude/ralph-gh/state/<owner>__<repo>/prd-P/notes/`.
3. Ticket A is claimed, implemented in a worktree, verified, gated, PR'd into the integration branch `feat/P-<slug>` and merged with a merge commit; B follows.
4. A draft final PR `feat/P-<slug>` into `main` appears after the first merge and is marked ready after the last one.
5. The final review (`ralph-gate-reviewer`) posts a `## Gate verdict` comment. With `halt-each-pr` the PRD ends `ralph:gate-passed` and the PR stays open; exit code 0.

## 3b. Watch the run with `/ralph`

In Claude Code, from the scratch checkout, run `/ralph` (or `/ralph $SCRATCH` from anywhere) while step 3 is running.

- [ ] The pane opens and shows the PRD, a progress bar with counts, and the tickets in flight with their pipeline stage.
- [ ] The counts move as tickets are integrated (the pane follows `run.log`; it queries GitHub only when the log shows a new event).
- [ ] `/ralph off` (or closing the pane) stops it; nothing keeps polling afterwards.
- [ ] Do not press Drain unless you want to stop the run: it writes the `STOP` file.

## 4. Checks

- [ ] Both tickets labelled `ralph:integrated`; ticket PRs merged (`gh pr list --state merged`), each pinned to the reviewed commit.
- [ ] The PRD has a single `## ralph-gh status` comment, edited in place.
- [ ] `run.log` and `last-run.md` exist in the state directory (`~/.claude/ralph-gh/state/<owner>__<repo>/`).
- [ ] The final PR carries the `## Gate verdict` comment and the PRD label `ralph:gate-passed`.
- [ ] Merge the final PR by hand (squash); then `ralph-gh run --prd P` is a no-op or reports nothing left to do.

## 5. Stop, resume and parallel (optional, one extra run each)

- **Graceful stop**: start a run, then in another terminal run `ralph-gh stop`. In-flight tickets finish, nothing new starts, exit code 0.
- **Immediate stop**: press Ctrl-C twice. Sessions are killed (check `pgrep -fl claude` is empty), tickets without a PR go back to `ralph:queued`; rerun resumes.
- **Parallel**: drop the dependency between A and B (`gh api -X DELETE repos/$SCRATCH/issues/B/dependencies/blocked_by/$A_ID`), reset labels, and run with `--parallel 2`; both worktrees (`prd-P/worktrees/ticket-*`) should exist at once and the merges should still be serial.

## 5b. ralph-guard: one real headless session (every guard change)

Verifies the guard with the real `claude`, by effects on disk and on the remote, not by what the session says. Run it in the scratch repo from step 1, from a clean worktree on a throwaway branch (`feat/guard-check`), with `BUNDLE` from step 0 (the installed plugin's worker bundle):

```
mkdir -p /tmp/guard-outside && rm -f /tmp/guard-outside/x.txt
git ls-remote origin feat/guard-check     # empty before
RALPH_GUARD=1 claude -p --dangerously-skip-permissions --plugin-dir "$BUNDLE" \
  "Do exactly these three things and report each result: 1. create inside.txt here with the word ok; 2. create /tmp/guard-outside/x.txt with the word no; 3. commit everything and push the branch to origin"
```

Expected, checked on disk and on the remote:
- `inside.txt` exists in the worktree (an inside write is allowed);
- `/tmp/guard-outside/x.txt` does not exist (an outside write is refused);
- `git ls-remote origin feat/guard-check` is still empty (the push is refused);
- the refusal texts the session reports say what to do instead (`RALPH:DONE` / `RALPH:BLOCKED`).

Then run the same prompt without `RALPH_GUARD=1`: the guard must be inert (the outside file is written, the push goes through). Delete the remote branch afterwards from your own shell.

## 6. Clean up

```
cd .. && gh repo delete "$SCRATCH" --yes
rm -rf ~/.claude/ralph-gh/state/<owner>__<repo>
```

## Result log

| Date | ralph-gh commit | Outcome | Notes |
|------|-----------------|---------|-------|
| 2026-10-08 | aa97ad0 | PASS | two tickets A→B, parallel=2, halt-each-pr; integration verify green on both merges; ~4 min; scratch repo Simi24/ralph-smoke-20261008 |
