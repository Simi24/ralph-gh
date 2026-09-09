# ralph-gh

A GitHub-issue-driven Ralph loop for [Claude Code](https://claude.com/claude-code).

Point it at a repo with a labeled issue backlog and walk away: a project-agnostic bash orchestrator spawns fresh `claude --print` sessions in series — one issue per iteration — with **all state living on GitHub** (labels, lease comments, PRs) and a **deterministic, non-skippable review gate** between every PR and `main`.

Built on the shoulders of:
- Geoffrey Huntley's **Ralph Wiggum** technique (the agent-in-a-loop idea)
- the [snarktank/ralph](https://github.com/snarktank/ralph) pattern (adapted here to GitHub issues instead of a `prd.json`)

## Why this instead of a hosted "assign-an-issue-to-AI" bot

- **Runs on your Claude Code subscription, locally.** No separate product, no API billing, no code leaving your machine except through your own git remotes.
- **Composes with your setup.** Your skills, your custom agents, your git hooks all apply inside every iteration.
- **Quality gates are control flow, not vibes.** The session that writes the code cannot merge it. Merging requires a `GATE:PASS` verdict produced by a *separate* reviewer session that the orchestrator spawns and parses — the same pattern as CI: deterministic bash, not instructions an LLM might skip.
- **The board never lies.** Labels are a real state machine (`queued → in-progress → needs-review → done | failed:*`) with lease comments, heartbeats, stale-lease recovery and dependency ordering (`Blocked by #N`). A watcher auto-claims labels if a session forgets the protocol.

## How it works

```
             ┌──────────────────────── orchestrator (bash) ────────────────────────┐
issues ──►   │  iteration session (fresh context)                                  │
ralph:queued │  SELECT → CLAIM → CONTEXT → IMPLEMENT (TDD + ralph-refactorer)      │
             │  → VERIFY (your commands) → PR → inner gate (advisory) → exit       │
             │                                                                     │
             │  external gate session (fresh context, ralph-gate-reviewer)         │
             │  ├─ GATE:PASS ──► orchestrator merges, labels ralph:done            │
             │  └─ GATE:FAIL ──► fix session → re-gate (×N) → ralph:failed:issue   │
             └─────────────────────────────────────────────────────────────────────┘
```

1. **Orchestrator** (`ralph-gh.sh`) does pre-flight checks (clean tree, on base branch, deps, labels, infra health) and loops.
2. **Each iteration** is a fresh `claude --dangerously-skip-permissions --print` session fed `CLAUDE.md` + per-repo runtime parameters. No context accumulates across iterations; persistence lives on GitHub.
3. **Inside the iteration**: SELECT a queued issue with resolved deps → CLAIM (label swap + lease comment + branch) → CONTEXT (issue, parent PRD, shadow-read merged sibling PRs) → IMPLEMENT in TDD, spawning the `ralph-refactorer` agent after each red-green cycle → VERIFY (your commands, in order) → open the PR → run the **inner gate** (`ralph-gate-reviewer` agent) and fix its findings in-context → emit a `<promise>` signal and exit. **The session never merges.**
4. **After the session exits**, the orchestrator runs the **external gate**: a fresh session spawns the same reviewer agent against the PR, posts the verdict as a `## Gate verdict` PR comment, and prints `GATE:PASS` or `GATE:FAIL` on its last line. PASS → the *script* merges (per autonomy mode). FAIL → the script spawns a focused fix session and re-gates, up to `RALPH_GATE_FIX_ROUNDS` times, then labels `ralph:failed:issue` for a human.

### The two companion agents

Installed user-level (`~/.claude/agents/`), so they work in every repo. Both pin `model: opus` in their frontmatter — the model is decided by the definition, not the caller — while iteration sessions run on the cheaper model you set in `.ralph-gh.config` (`export ANTHROPIC_MODEL=...`).

- **`ralph-refactorer`** — the REFACTOR step of each TDD cycle: improves the code just written in the GREEN phase without changing behavior, re-running tests after every step.
- **`ralph-gate-reviewer`** — the review gate: adversarial correctness pass, acceptance-criteria coverage table, repo-standards compliance, structured `PASS`/`FAIL` verdict. If a two-axis code-review skill is installed (e.g. Matt Pocock's `code-review`, or a derivative) it drives the review with it; otherwise it degrades gracefully to its built-in process.

A repo can override either by defining its own agent and saying so in its `AGENTS.md` — the repo always wins.

## Install

```bash
git clone <this-repo>
cd ralph-gh
./install.sh

# optional alias
echo 'alias ralph-gh="$HOME/.claude/ralph-gh/ralph-gh.sh"' >> ~/.zshrc
```

The installer also registers a `/ralph-gh` skill, so inside an interactive Claude Code session you can type `/ralph-gh --autonomy=... --max-iterations=...` and have the session drive the orchestrator for you.

Requires: `claude` (Claude Code CLI), `gh` (authenticated), `jq`, bash 4+.

## Per-repo setup (one time)

```bash
cd /path/to/your-repo
cp ~/.claude/ralph-gh/example.ralph-gh.config .ralph-gh.config
$EDITOR .ralph-gh.config
```

Fill in:
- `RALPH_VERIFY_COMMANDS` — build/test commands, run in order every iteration (required)
- `RALPH_PREFLIGHT_CMD` + `RALPH_PREFLIGHT_HEALTH_URL` — if the tests need infra (docker compose, LocalStack, …)
- `RALPH_YOLO_ALLOWLIST` — regex of files allowed to auto-merge in `yolo` mode
- `RALPH_DOC_FILES` — docs to update on public-API changes
- `RALPH_BRANCH_PREFIX`, `RALPH_DEFAULT_BASE_BRANCH` — if your conventions differ
- `RALPH_GATE_FIX_ROUNDS` — external-gate fix rounds before giving up (default 2)
- `export ANTHROPIC_MODEL=...` — model for iteration sessions (gates stay on Opus regardless)

Then label your backlog:

```bash
gh issue edit N --add-label ralph:queued
gh issue edit M --add-label ralph:queued,ralph:hitl-arch   # sensitive: never auto-merge
```

Issues can declare dependencies in their body (`Blocked by #N`) — the loop won't pick them up until #N is closed.

## Usage

```bash
cd /path/to/your-repo

ralph-gh                                            # default: respect-hitl-arch, 20 iterations
ralph-gh --autonomy=halt-each-pr --max-iterations=1 # conservative: one PR, then stop
ralph-gh --autonomy=respect-hitl-arch --max-iterations=15   # night mode
ralph-gh --autonomy=yolo --max-iterations=10        # auto-merge within the allowlist
```

For overnight runs wrap it in tmux (it is not a daemon):

```bash
tmux new -s ralph 'cd /path/to/repo && caffeinate -dims ralph-gh --max-iterations=15'
```

## Autonomy modes

| Mode | Behavior |
|---|---|
| `halt-each-pr` | One PR per run; the external gate still posts its verdict, merge is yours. |
| `respect-hitl-arch` (default) | Orchestrator auto-merges on external-gate PASS, unless the issue has `ralph:hitl-arch` (then it halts after the PR, verdict posted). |
| `yolo` | Auto-merges on PASS **if** every changed file matches `RALPH_YOLO_ALLOWLIST` (checked deterministically by the script). |

## Label semantics

| Label | Meaning |
|---|---|
| `ralph:queued` | Ready to work, deps satisfied |
| `ralph:in-progress` | Active iteration; stale > 10 min → released by the next session |
| `ralph:needs-review` | PR open, waiting on the gate or a human |
| `ralph:hitl-arch` (manual) | Architecturally sensitive, never auto-merge |
| `ralph:done` | Merged by the orchestrator |
| `ralph:failed:systemic` | Tooling/infra failure → loop stops |
| `ralph:failed:issue` | Per-issue failure (including exhausted gate-fix rounds) → loop continues |

## Stop signals (session → orchestrator)

| Signal | Action |
|---|---|
| `<promise>CONTINUE</promise>` | Spawn the next iteration |
| `<promise>QUEUE_EMPTY</promise>` | Stop — nothing left |
| `<promise>CASCADE_FAIL</promise>` | Stop — all candidates blocked by a failed issue |
| `<promise>SYSTEMIC_FAIL</promise>` | Stop — infra broken |
| `<promise>HALT</promise>` | Stop — awaiting a human decision |

No signal = fail-soft, next iteration anyway. External gates run **before** the signal is honored, so an open PR always gets its verdict.

## State

- **GitHub**: labels, lease comments, PR comments (`## Gate verdict`) — the source of truth
- **Git**: commits and merged PRs — the outcome
- **Local, ephemeral** (`.ralph-gh/`, auto-gitignored): `run.log`, `last-run.md`, per-iteration input/output, per-gate input/output

## Recommended companions

The loop shines with these (not bundled — install them yourself, from [Matt Pocock's skills](https://github.com/mattpocock/skills)):

- **`wayfinder`** — turns an idea into a mapped backlog of issues with acceptance criteria (perfect upstream of ralph-gh)
- **`tdd`** — the red-green-refactor discipline the iterations follow
- **`code-review`** — the two-axis (Standards/Spec) review; the gate agent auto-detects it, or any derivative of it, and falls back to an inline version otherwise

## Security

Iterations run with `--dangerously-skip-permissions`: the session can run any command your user can. Treat it accordingly:

- prefer running inside a **devcontainer** or throwaway VM for unattended runs;
- keep protective **git hooks** in the repo (block force-push, protected branches, destructive `gh` operations);
- the orchestrator refuses to start on a dirty tree or off the base branch, and never edits issue bodies.

## What it does NOT do

- Does NOT create issues — bring your own backlog (see `wayfinder` / a PRD-to-issues flow)
- Does NOT edit issue bodies (only comments)
- Does NOT stop your infra on exit
- Does NOT run as a daemon — use tmux/screen for overnight

## License

MIT
