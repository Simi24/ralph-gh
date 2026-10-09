# Smoke test: a two-ticket PRD on a scratch repository

This is the end-to-end check of the Python conductor against the real GitHub and the real `claude` CLI. It is **not** run by the unit tests and **not** run automatically: it creates a GitHub repository and spends real Claude sessions (a few ordinary sessions plus two reviewers), so a human approves and runs it.

Status: executed once, see the result log at the bottom. Record each further run there.

## Prerequisites

- `gh` authenticated with the `repo` and `delete_repo` scopes (`gh auth refresh -s delete_repo`), `git`, `claude` on `PATH`, Python 3.12+.
- This checkout of ralph-gh, with `agents/ralph-ticket-gate.md` and `agents/ralph-gate-reviewer.md`, which step 1 commits into the scratch repo's `.claude/agents/`. Preflight refuses to start without both. Do not copy them into `~/.claude/agents/`: that overwrites a live ralph-gh install.
- Native sub-issues and dependencies enabled (they are on by default on github.com).

Set two variables used below (adjust the owner):

```
export RALPH_SRC=/path/to/ralph-gh        # this checkout
export SCRATCH=<your-gh-user>/ralph-smoke # the scratch repo
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

Copy the two agents into the scratch repo: `mkdir -p .claude/agents && cp "$RALPH_SRC"/agents/ralph-ticket-gate.md "$RALPH_SRC"/agents/ralph-gate-reviewer.md .claude/agents/`. Preflight looks in the project's `.claude/agents/` too, and project agents take precedence over user agents of the same name.

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
python3 "$RALPH_SRC/ralph-gh" run --prd P --autonomy halt-each-pr
```

Run it from anywhere inside the scratch checkout. Expected, in order:

1. Preflight passes (tools, auth, permissions, clean tree, both agents found, labels created).
2. Exploration notes are written under `~/.claude/ralph-gh/state/<owner>__<repo>/prd-P/notes/`.
3. Ticket A is claimed, implemented in a worktree, verified, gated, PR'd into the integration branch `feat/P-<slug>` and merged with a merge commit; B follows.
4. A draft final PR `feat/P-<slug>` into `main` appears after the first merge and is marked ready after the last one.
5. The final review (`ralph-gate-reviewer`) posts a `## Gate verdict` comment. With `halt-each-pr` the PRD ends `ralph:gate-passed` and the PR stays open; exit code 0.

## 4. Checks

- [ ] Both tickets labelled `ralph:integrated`; ticket PRs merged (`gh pr list --state merged`), each pinned to the reviewed commit.
- [ ] The PRD has a single `## ralph-gh status` comment, edited in place.
- [ ] `run.log` and `last-run.md` exist in the state directory (`~/.claude/ralph-gh/state/<owner>__<repo>/`).
- [ ] The final PR carries the `## Gate verdict` comment and the PRD label `ralph:gate-passed`.
- [ ] Merge the final PR by hand (squash); then `ralph-gh run --prd P` is a no-op or reports nothing left to do.

## 5. Stop, resume and parallel (optional, one extra run each)

- **Graceful stop**: start a run, then in another terminal run `python3 "$RALPH_SRC/ralph-gh" stop`. In-flight tickets finish, nothing new starts, exit code 0.
- **Immediate stop**: press Ctrl-C twice. Sessions are killed (check `pgrep -fl claude` is empty), tickets without a PR go back to `ralph:queued`; rerun resumes.
- **Parallel**: drop the dependency between A and B (`gh api -X DELETE repos/$SCRATCH/issues/B/dependencies/blocked_by/$A_ID`), reset labels, and run with `--parallel 2`; both worktrees (`prd-P/worktrees/ticket-*`) should exist at once and the merges should still be serial.

## 6. Clean up

```
cd .. && gh repo delete "$SCRATCH" --yes
rm -rf ~/.claude/ralph-gh/state/<owner>__<repo>
```

## Result log

| Date | ralph-gh commit | Outcome | Notes |
|------|-----------------|---------|-------|
| 2026-10-08 | aa97ad0 | PASS | two tickets A→B, parallel=2, halt-each-pr; integration verify green on both merges; ~4 min; scratch repo Simi24/ralph-smoke-20261008 |
