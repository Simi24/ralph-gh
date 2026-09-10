# ralph-gh

A GitHub-issue-driven Ralph loop for [Claude Code](https://claude.com/claude-code).

Point it at a repo with a labeled issue backlog and walk away: a project-agnostic bash orchestrator spawns fresh `claude --print` sessions in series — one issue per iteration — with **all state living on GitHub** (labels, lease comments, PRs) and a **deterministic, non-skippable review gate** between every PR and `main`.

Built on the shoulders of:
- Geoffrey Huntley's **Ralph Wiggum** technique (the agent-in-a-loop idea)
- the [snarktank/ralph](https://github.com/snarktank/ralph) pattern (adapted here to GitHub issues instead of a `prd.json`)

## Why this instead of a hosted "assign-an-issue-to-AI" bot

- **Runs on your Claude Code subscription, locally.** No separate product to buy and nothing hosting your repo: sessions run on your machine against the Anthropic API, and code moves only through your own git remotes.
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

1. **Orchestrator** (`ralph-gh.sh`) does pre-flight checks (clean tree, on base branch, deps, repo permissions, labels, infra health) and loops.
2. **Each iteration** is a fresh `claude --dangerously-skip-permissions --print` session fed `CLAUDE.md` + per-repo runtime parameters. No context accumulates across iterations; persistence lives on GitHub.
3. **Inside the iteration**: SELECT a queued issue with resolved deps → CLAIM (label swap + lease comment + branch) → CONTEXT (issue, parent PRD, shadow-read merged sibling PRs) → IMPLEMENT in TDD, spawning the `ralph-refactorer` agent after each red-green cycle → VERIFY (your commands, in order) → open the PR → run the **inner gate** (`ralph-gate-reviewer` agent) and fix its findings in-context → emit a `<promise>` signal and exit. **The session never merges.**
4. **After the session exits**, the orchestrator runs the **external gate**: a fresh session spawns the same reviewer agent against the PR, posts the verdict as a `## Gate verdict` PR comment, and prints `GATE:PASS` or `GATE:FAIL` on its last line. PASS → the *script* merges (per autonomy mode). FAIL → the script spawns a focused fix session and re-gates, up to `RALPH_GATE_FIX_ROUNDS` times, then labels `ralph:failed:issue` for a human.

### The two companion agents

Installed user-level (`~/.claude/agents/`), so they work in every repo. Both pin `model: opus` in their frontmatter — the model is decided by the definition, not the caller — while iteration sessions (and the thin wrapper sessions that drive the gate and the fixes) run on the model you set in `.ralph-gh.config` (`export ANTHROPIC_MODEL=...`); the review judgment itself always happens inside the Opus-pinned agent.

- **`ralph-refactorer`** — the REFACTOR step of each TDD cycle: improves the code just written in the GREEN phase without changing behavior, re-running tests after every step.
- **`ralph-gate-reviewer`** — the review gate: adversarial correctness pass, acceptance-criteria coverage table, repo-standards compliance, structured `PASS`/`FAIL` verdict. If the user has a code-review skill installed, the gate drives the review with it; otherwise it degrades gracefully to its built-in two-axis process.

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

Requires: `claude` (Claude Code CLI), `gh` (authenticated as a collaborator with **push** and **triage** permission — or higher, e.g. maintain/admin — on the repo, so it can push branches, merge PRs, and create/edit `ralph:*` labels), `jq`, `curl`, bash 3.2+. The orchestrator checks this for real at startup (`gh api repos/{owner}/{repo} --jq .permissions`) and exits before spawning any session if either is missing.

## Per-repo setup (one time)

```bash
cd /path/to/your-repo
cp ~/.claude/ralph-gh/example.ralph-gh.config .ralph-gh.config
$EDITOR .ralph-gh.config
```

Fill in:
- `RALPH_VERIFY_COMMANDS` — build/test commands, run in order every iteration (required)
- `RALPH_PREFLIGHT_CMD` + `RALPH_PREFLIGHT_HEALTH_URL` — if the tests need infra (docker compose, LocalStack, …); a failed command or a health check that never goes green aborts the run before any session is spawned. Each health probe is capped at 3s connect / 5s total, so the worst case is `RALPH_PREFLIGHT_HEALTH_RETRIES × 6s`; the retry count must be a positive integer or the run aborts at startup
- `RALPH_YOLO_ALLOWLIST` — regex of files allowed to auto-merge in `yolo` mode
- `RALPH_DOC_FILES` — docs to update on public-API changes
- `RALPH_BRANCH_PREFIX`, `RALPH_DEFAULT_BASE_BRANCH` — if your conventions differ
- `RALPH_GATE_FIX_ROUNDS` — external-gate fix rounds before giving up (default 2)
- `RALPH_SESSION_TIMEOUT` — seconds before a hung session is killed (default 7200)
- `RALPH_WAIT_FOR_RESET`, `RALPH_USAGE_WAIT_SECONDS` — usage-limit behavior, see [Usage-limit awareness](#usage-limit-awareness) (defaults: exit on a hit, no wait)
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
| `ralph:needs-review` | PR open, awaiting the external gate (the gate only processes PRs in this state). **Self-healing**: if a human closes or merges the PR outside the gate, the orchestrator's reconcile step relabels it (`ralph:done` if merged, `ralph:queued` to retry if closed unmerged) instead of leaving it stuck forever. |
| `ralph:gate-passed` | External gate PASS, merge withheld for a human (hitl-arch, halt-each-pr, or yolo allowlist miss). **Self-healing**: if a human merges the withheld PR, the reconcile step relabels it `ralph:done`. |
| `ralph:hitl-arch` (manual) | Architecturally sensitive, never auto-merge |
| `ralph:done` | Merged by the orchestrator (or reconciled after a human merge) |
| `ralph:failed:systemic` | Tooling/infra failure → loop stops |
| `ralph:failed:issue` | Per-issue failure (including exhausted gate-fix rounds) → loop continues, PR left open but never re-processed |

Neither `ralph:failed:*` label is ever applied for a session that died because the *account* hit a usage limit, not because of anything wrong with the work — see [Usage-limit awareness](#usage-limit-awareness).

Reconciliation runs at the start of every gate pass (`reconcile_board_states`, the first step inside `run_external_gates`): it resolves the PR linked to an issue via GitHub's own closing-keyword linkage (not branch-name matching, so it still works even if the PR's branch never followed the `issue-N` convention), and fails closed — a `gh`/`jq` error is logged (`[reconcile]` prefix in `run.log`) and the issue is left untouched rather than guessing a transition.

The external gate itself (`run_external_gates`) resolves a candidate issue number from the PR's branch name or, failing that, its body's closing keyword (`Closes #N` and friends), then — when possible — confirms that candidate against the same closing-keyword linkage before treating the PR as that issue's PR. GitHub only populates this linkage against a PR whose base is the repo's *actual* default branch. If your `RALPH_DEFAULT_BASE_BRANCH` is a supported override that differs from it (e.g. developing off `develop` while GitHub's default is `main`), linkage can never exist for any PR; the gate detects that once per pass, logs `[gate] closing-keyword linkage unavailable ...`, and falls back to the unconfirmed candidate, relying on the `ralph:needs-review` label check as the eligibility boundary instead.

## Stop signals (session → orchestrator)

| Signal | Action |
|---|---|
| `<promise>CONTINUE</promise>` | Spawn the next iteration |
| `<promise>QUEUE_EMPTY</promise>` | Stop — nothing left |
| `<promise>CASCADE_FAIL</promise>` | Stop — all candidates blocked by a failed issue |
| `<promise>SYSTEMIC_FAIL</promise>` | Stop — infra broken |
| `<promise>HALT</promise>` | Stop — awaiting a human decision |

No signal = fail-soft, next iteration anyway. External gates run **before** the signal is honored, so an open PR always gets its verdict.

## Stopping a run (operator → orchestrator)

The loop is not a daemon (see [What it does NOT do](#what-it-does-not-do)), but killing the terminal it runs in used to be the only way to stop it early — the in-flight session died mid-iteration, `last-run.md` never got its Final section, and the claimed issue sat `ralph:in-progress` until the stale-lease rule released it. Two deliberate stop levels replace that:

| Trigger | Level | Effect |
|---|---|---|
| `touch .ralph-gh/STOP`, or a first `Ctrl-C` (SIGINT) | **Graceful** | The in-flight iteration and its external-gate pass finish normally; no new issue is claimed; the loop then exits (`stopped by operator`). |
| A second `Ctrl-C`, or `SIGTERM` | **Immediate** | The running `claude` session is killed right away; if its issue is still `ralph:in-progress`, it's returned to `ralph:queued` with a lease-release comment; the loop exits (`stopped by operator (immediate)`). |

`last-run.md` gets its `## Final` section on every exit path once a session has started — normal completion, either stop level, or a mid-run error — not just the happy path. (A startup abort — bad config, missing deps, a preflight that never goes green — happens before any session exists and leaves the previous run's `last-run.md` untouched, same as before this feature.) A stop file left over from a previous run is removed at startup, so it can never block a new run.

## Usage-limit awareness

Claude.ai subscription plans (Pro, Max, Team, Enterprise) expose no API to check remaining quota in the rolling session/weekly window, so ralph-gh cannot predict whether a run will finish before hitting one — any "will this fit in my quota?" check would be a guess dressed up as a check, and this project doesn't promise what it can't enforce. What it *can* do is react once a session has already reported hitting the wall, instead of misfiling a perfectly good issue as failed because the account ran dry mid-session.

`detect_usage_limit` (one function, `ralph-gh.sh`) checks an iteration/gate/fix session's raw JSON `errors` array and stderr — never its free-form conversational reply — for the CLI's documented usage-limit wording (`You've hit your session limit`, `...weekly limit`, `...Opus limit`, ...). Scanning only the CLI/API-level diagnostics, not the model's own text, is deliberate: a session merely *discussing* usage limits (this feature's own review is a real example) can say the trigger phrase without an actual limit ever being hit, and the model's reply is the one part of a session's output that can be steered into saying anything. This wording is **not a stable API** — Anthropic can reword it without notice, and whether a headless `--print` session even reuses the interactive CLI's exact phrasing is unconfirmed — so the match is best-effort: anything that doesn't match falls straight through to today's existing "no parsable result" handling, never a guess.

On a match:

- Neither `ralph:failed:systemic` nor `ralph:failed:issue` is ever applied.
- An iteration-session hit requeues the claimed issue to `ralph:queued` (the same lease-release path used by an immediate stop, see above). A gate/fix-session hit leaves the PR's issue exactly `ralph:needs-review` — an open PR already exists, so it's simply re-gated on a later pass instead of being requeued into a state that implies no PR exists yet.
- **Default** (`RALPH_WAIT_FOR_RESET=0`): the run exits cleanly, and `last-run.md`'s exit reason records the best-effort reset description ralph-gh could parse out of the session's own output (`usage limit — resets at <description>`, or `unknown (not stated in session output)` when none was present).
- **`RALPH_WAIT_FOR_RESET=1`**: instead of exiting, ralph-gh sleeps `RALPH_USAGE_WAIT_SECONDS` (default 1800) and resumes the loop. This is a bounded wait, not a precise sleep-until-reset — the reset time, when a session states one at all, is prose in a locale/format ralph-gh doesn't try to parse into a schedule. If the limit hasn't actually lifted yet, the next attempt hits the same detection and waits again; each round is still bounded by `RALPH_USAGE_WAIT_SECONDS` and by `--max-iterations` overall.

## State

- **GitHub**: labels, lease comments, PR comments (`## Gate verdict`) — the source of truth
- **Git**: commits and merged PRs — the outcome
- **Local, ephemeral** (`.ralph-gh/`, auto-gitignored): `run.log` (appended across runs), `last-run.md` (rewritten each run) and `STOP` (a one-shot stop-request file, `touch`-able from another terminal, removed at startup and once consumed), plus per-iteration input/output/working notes and per-gate input/output — these last session-scoped (`<name>.<session>.*`) so two runs never overwrite each other's artifacts, same convention as `touched-issues.<session>.txt` (the session-scoped record behind `last-run.md`'s "Issues touched this session") — the `.output.txt` files hold only the clean final-result text; sibling `.raw.json`/`.stderr.log` files carry the full CLI response and stderr for debugging

## Recommended companions

The loop shines with these (not bundled — install them yourself, from [Matt Pocock's skills](https://github.com/mattpocock/skills)):

- **`wayfinder`** — turns an idea into a mapped backlog of issues with acceptance criteria (perfect upstream of ralph-gh)
- **`tdd`** — the red-green-refactor discipline the iterations follow
- **`code-review`** — the two-axis (Standards/Spec) review; the gate agent auto-detects it, or any derivative of it, and falls back to an inline version otherwise

## Security — read this before an unattended run

**Threat model first**: ralph-gh is designed for private repos, or repos where everyone with write access is trusted. Two things keep a public repo from being an open door — only issues labeled `ralph:queued` enter the loop (labeling requires triage access), and fork PRs never enter the gate pipeline — but issue bodies and PR comments written by anyone still reach your sessions as text. If you run it on a public repo, never label a stranger's issue without rewriting it in your own words, and prefer short supervised runs over overnight ones.

Every session (iteration, gate, fix) runs with `--dangerously-skip-permissions` and your `gh` credentials: it can run any command your user can. Be clear about what is and is not enforced:

- **Deterministic (bash):** which PRs enter the pipeline (same-repo branches whose issue is `ralph:needs-review` — fork PRs are excluded by design), verdict parsing (an unparsable verdict is a FAIL, never a PASS — and so is any verdict left behind by a session the orchestrator had to kill at timeout), the yolo allowlist check (fails closed if the diff cannot be fetched), the merge itself, session timeouts.
- **Prompt-level only:** everything the CLAUDE.md tells a session not to do (no merging, no force-push, no dependency changes). A misbehaving or prompt-injected session is not technically prevented from ignoring these; the external gate exists to catch the *output* of such a session before it reaches your base branch, not to make the session itself safe.
- **Untrusted input:** issue bodies, PR comments and code under review are attacker-controlled text that flows into permissionless sessions. The orchestrator passes gate findings to fix sessions from its own captured output rather than trusting PR comments, and fix sessions are told to treat GitHub content as data — but that is still an instruction, not a guarantee.

Practical rules:

- for unattended runs, prefer a **devcontainer** or throwaway VM: that is the actual security boundary, not the prompts;
- be careful on **public repos**: anyone can open issues and comment on PRs, and that text reaches your sessions;
- note that `.ralph-gh.config` is `source`d and `RALPH_PREFLIGHT_CMD` is `eval`'d by the orchestrator — anyone with write access to the repo can put shell in them, so treat config changes like code review;
- keep protective **git hooks** in the repo (block force-push, protected branches, destructive `gh` operations);
- the orchestrator refuses to start on a dirty tree or off the base branch, stops if an iteration leaves the tree dirty, and never edits issue bodies.

## What it does NOT do

- Does NOT create issues — bring your own backlog (see `wayfinder` / a PRD-to-issues flow)
- Does NOT edit issue bodies (only comments)
- Does NOT stop your infra on exit
- Does NOT run as a daemon — use tmux/screen for overnight

## License

MIT
