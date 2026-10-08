# ralph-gh

A GitHub-PRD-driven Ralph loop for [Claude Code](https://claude.com/claude-code).

Point it at a PRD issue whose sub-issues are your tickets and walk away: a Python conductor (standard library only) spawns fresh `claude --print` sessions — one per ticket, several at once — merges each ticket into one integration branch, then reviews and merges a single final PR into your base branch. **All state lives on GitHub** (labels, PRs, comments, native sub-issues and dependencies) and a **deterministic, non-skippable review** sits between the work and your base branch.

Built on the shoulders of:
- Geoffrey Huntley's **Ralph Wiggum** technique (the agent-in-a-loop idea)
- the [snarktank/ralph](https://github.com/snarktank/ralph) pattern (adapted here to GitHub issues instead of a `prd.json`)

## Why this instead of a hosted "assign-an-issue-to-AI" bot

- **Runs on your Claude Code subscription, locally.** No separate product to buy and nothing hosting your repo: sessions run on your machine against the Anthropic API, and code moves only through your own git remotes.
- **Composes with your setup.** Your skills, your custom agents, your git hooks all apply inside every session.
- **Quality gates are control flow, not vibes.** The session that writes the code cannot merge it. Merging requires a `GATE:PASS` verdict produced by a *separate* reviewer session that the conductor spawns and parses — the same pattern as CI: deterministic code, not instructions an LLM might skip.
- **The board never lies.** Labels are a real state machine (`queued → in-progress → in-review → integrated → done | failed | blocked`), re-read from GitHub on every start, so a stopped or crashed run resumes from what is actually there.

## The PRD workflow

You bring the backlog: a **PRD issue** whose **sub-issues** are the tickets, with real GitHub **dependencies** (`blocked by`) between tickets that must go in order. Label the tickets `ralph:queued`, then run `ralph-gh run --prd N`.

```
PRD #N ──► exploration session (once: notes on the code the tickets touch)
   │
   ├─ ticket A ─┐  each ticket, in its own worktree and its own claude session:
   ├─ ticket B ─┼─ implement (TDD) → verify (your commands) → push + PR into the
   ├─ ticket C ─┘  integration branch → light ticket gate → merge (serial)
   │                 └─ verify or gate FAIL → fix session → re-verify / re-gate (×N)
   ▼
integration branch  feat/<N>-<slug>   (draft final PR opened after the first merge)
   │
   ▼
final review (ralph-gate-reviewer, Opus) of the whole PR against the PRD
   ├─ PASS ──► autonomy mode decides: squash-merge, or withhold for a human
   └─ FAIL ──► final-fix session → scoped re-review (×N) → ralph:blocked
```

1. **Startup.** The conductor takes the per-repo run lock, runs preflight (see [Install](#install) for the requirements; it also checks the tree is clean, both agents exist, creates the `ralph:*` labels that are missing, and runs your optional preflight command and health check), reads the PRD's sub-issues and dependencies, refuses a dependency cycle, and **reconciles the board** with reality (a ticket whose PR a human merged becomes `ralph:integrated`; one whose PR was closed unmerged, or that has no PR, goes back to `ralph:queued`; an open PR resumes at verify, gate and merge). It then creates the integration branch `<branch_prefix>/<prd>-<slug>` off `base_branch`.
2. **Exploration.** One session per PRD maps the code the tickets touch and writes short notes outside the repo. Every writing session gets a pointer to that file, so parallel tickets share one map instead of each re-exploring. If exploration fails the run goes on without notes.
3. **The frontier.** Tickets are dispatched as soon as their blockers are satisfied (a blocker in the PRD is satisfied when it is `ralph:integrated`; one outside the PRD when it is closed), up to `parallel` at a time, `ralph:hitl-arch` tickets last and ascending by number. Each ticket runs in its own git worktree on branch `<branch_prefix>/<prd>-ticket-<n>`.
4. **Per ticket.** An implementer session builds it test-first and ends with `RALPH:DONE` (or `RALPH:BLOCKED <reason>`, which labels the ticket `ralph:blocked` for a human). The *conductor* then runs your `verify_commands`, pushes, opens the PR into the integration branch (`ralph:in-review`) and runs the **light ticket gate** (`ralph-ticket-gate`, Sonnet): a fresh session that checks only the ticket's acceptance criteria and prints `GATE:PASS` or `GATE:FAIL`. A verify failure or a gate FAIL starts a **fix session** (given only the failing output or the BLOCKING findings), then the ticket is re-verified and re-gated, at most `gate_fix_rounds` times in total; after that the ticket is `ralph:failed:issue` with a diagnosis comment and its PR left open. On PASS the *conductor* merges the PR with a merge commit, pinned with `--match-head-commit` to the reviewed commit, and labels the ticket `ralph:integrated`. If the integration branch moved and the PR no longer merges, a **merge-fix session** resolves the conflict (bounded by `gate_fix_rounds`), the ticket is re-verified, and the **ticket gate runs again** before the merge, because a bad resolution could drop the code of an already integrated ticket. The gate is told which merge commit resolved the conflict, and every ticket gate (not only that one) treats removing or rewriting integration-branch code the ticket does not need as BLOCKING; a FAIL goes through the normal fix rounds. This matters when `parallel` is above 1, the only case where the integration branch moves while a ticket is in flight.
5. **Failures do not stop the run.** A failed or blocked ticket is left for a human and everything independent of it carries on. When nothing can be dispatched any more, the run ends incomplete (exit 3) and names the failed ticket, or the tickets cascade-blocked by it. A `git` or `gh` failure inside one ticket (or Ctrl-C landing mid-push: those tools run detached from the terminal's process group) ends just that ticket as `ralph:failed:systemic`, never as a traceback. When a human closes a ticket's PR without merging and the ticket is redone, the conductor first deletes the stale remote ticket branch (GitHub keeps the closed PR's commits under `refs/pull/N/head`) instead of force-pushing over it.
6. **The final PR.** After the first ticket merges, a **draft** PR `integration → base_branch` is opened (title from the PRD, a conventional-commit title, body with `Closes #` lines). Once every open ticket is `ralph:integrated` (a sub-issue a human closed, for example as wontfix, is ignored and gets no `Closes` line; an open sub-issue that never got a ralph label still blocks, and the exit reason names it), the **final review** runs *as* the reviewer agent (`claude --agent`) in a throwaway worktree: it reviews the whole PR against the PRD along two axes (standards and spec), adversarially. The conductor posts its verdict as a `## Gate verdict` PR comment. On FAIL a final-fix session gets only the BLOCKING findings, and the next review is scoped to the fix diff when that is provably safe (the reviewed commit is an ancestor of the new head and the previous verdict is available), otherwise it is a full review. After `gate_fix_rounds` failed rounds the PRD is `ralph:blocked`.
7. **Merge by autonomy mode.** On PASS the PR is marked ready and the [autonomy mode](#autonomy-modes) decides whether the conductor merges it (**squash**, one conventional commit on the base branch for release tooling, pinned to the reviewed head) or withholds it. After a merge the conductor closes the PRD and its tickets itself (closing keywords do nothing when the base is not the repo's default branch) and labels them `ralph:done`.

A review verdict tags every finding `BLOCKING` (a verified defect, an unmet acceptance criterion, a hard violation of a documented rule, a failing verify command) or `FOLLOW-UP` (judgement calls, nice-to-haves). Only `BLOCKING` findings FAIL a review or reach a fix session; `FOLLOW-UP` ones stay in the verdict comment for a human. An unparsable or missing verdict is never a PASS: the review is retried once with a fresh session, and a second unparsable verdict fails the ticket (or blocks the PRD) without a fix session.

### The two reviewer agents

Installed user-level (`~/.claude/agents/`, or `$CLAUDE_CONFIG_DIR/agents/`), so they work in every repo. Both pin their model in their frontmatter — the model is decided by the definition, not the caller — so reviewer sessions never get `--model`. Writing sessions (exploration, implementer, fix, merge-fix, final-fix) run on the `model` you set in `.ralph-gh.toml`, or on Claude Code's default.

- **`ralph-ticket-gate`** (Sonnet) — the light per-ticket gate: acceptance criteria and spec only, no standards or design review.
- **`ralph-gate-reviewer`** (Opus) — the final review of the PRD: adversarial correctness pass, acceptance-criteria coverage, repo-standards compliance, structured `PASS`/`FAIL` verdict. If you have a code-review skill installed it drives the review with it; otherwise it falls back to its built-in two-axis process. Review depth scales with what the diff touches (docs-only gets a lighter pass than core code), but the two-axis review and the AC coverage table are a floor at every tier. On a re-review it runs scoped to the fix diff.

A repo can use its own reviewers: set `reviewer_agent` / `ticket_gate_agent` in `.ralph-gh.toml` (the run stops at startup if no agent with that frontmatter `name:` exists in the user or repo agents directory). A repo can also declare its own critical paths in `AGENTS.md` (code whose failure corrupts state, authorizes actions, or handles untrusted input) to force the reviewer's deepest tier on any diff that touches them.

## Install

```bash
git clone <this-repo>
cd ralph-gh
./install.sh

# optional alias (the installer prints the exact line)
echo 'alias ralph-gh="$HOME/.claude/ralph-gh/ralph-gh"' >> ~/.zshrc
```

`install.sh` deploys, under `$CLAUDE_CONFIG_DIR` (default `~/.claude`): the `conductor/` package and the `ralph-gh` launcher into `ralph-gh/`, the two agents into `agents/`, and the `/ralph-gh` skill into `skills/ralph-gh/`. The skill lets you type `/ralph-gh --prd N ...` inside an interactive Claude Code session and have the session drive the conductor for you.

Requires: **Python 3.12+** (standard library only, nothing to `pip install`), `claude` (Claude Code CLI), `git`, and `gh` authenticated as a collaborator with **push** and **triage** permission — or higher — on the repo, so it can push branches, merge PRs, and create/edit `ralph:*` labels. Preflight checks all of this for real at startup and exits before spawning any session if something is missing.

## Updating

The copies under `~/.claude/` are a **deployment** of this repo, not a separate thing — a `git pull` alone changes nothing your runs actually use. Redeploy after every pull:

```bash
git pull
./install.sh
```

`install.sh` overwrites the installed copy and, for every installed file that differs from the clone's, first saves the old version as `*.bak` (`config.py.bak`, `ralph-gate-reviewer.md.bak`, ...). Files an older version installed and this one no longer ships (the bash orchestrator `ralph-gh.sh`, its `CLAUDE.md` iteration prompt, the `ralph-refactorer` agent, modules removed from the package) are moved aside as `.bak` too, so nothing stale stays active.

Each install stamps the clone's path and commit SHA into `ralph-gh/.installed`. If you `git pull` and then start a run without reinstalling, the conductor notices its installed copy is behind that clone and prints one line at startup (`WARNING: installed copy is behind your clone (<sha> -> <sha>) — run install.sh`) — advisory only, it never blocks the run, and it fails open when the clone is gone or not a git checkout. It never touches the network.

## Per-repo setup (one time)

```bash
cd /path/to/your-repo
cp ~/.claude/ralph-gh/example.ralph-gh.toml .ralph-gh.toml
$EDITOR .ralph-gh.toml
```

`.ralph-gh.toml` is plain data read with `tomllib`: nothing in it is evaluated. Only `verify_commands` is required; `example.ralph-gh.toml` documents every key with its default. Unknown keys and wrong types stop the run at startup.

- `verify_commands` — build/test commands, run in order in each ticket's worktree (required)
- `branch_prefix`, `base_branch` — if your conventions differ (`feat`, `main`)
- `parallel` — tickets worked at once (default 3); see [Parallelism and its usage cost](#parallelism-and-its-usage-cost)
- `gate_fix_rounds` — fix rounds before giving up, per ticket and for the final review (default 2)
- `reviewer_agent`, `ticket_gate_agent` — the agents the final review and the ticket gate run as
- `doc_files` — docs that must stay in sync with behavior (e.g. `["README.md"]`); implementer and fix sessions get one pointer line naming them, never their content
- `session_timeout` — seconds before a hung session is killed (default 7200)
- `model` — model for writing sessions (reviewers stay on their pinned model)
- `yolo_allowlist` — regexes of files allowed to auto-merge in `yolo` mode. Entries are Python regexes matched with `re.search`, i.e. **unanchored**: `README\.md` also matches `docs/README.md`. Anchor them with `^` (and `$`), e.g. `^README\.md$`
- `preflight_command`, `preflight_health_url`, `preflight_health_retries` — if the tests need infra (docker compose, LocalStack, ...); a failed command or a health check that never goes green aborts the run before any session is spawned. Each health probe is capped at 3s connect / 5s total
- `wait_for_reset`, `usage_wait_seconds` — see [Usage limits](#usage-limits)

**Migrating from the bash version:** a repo with only the old `.ralph-gh.config` is refused at startup with a table mapping every old `RALPH_*` variable to its new key. `--max-iterations` has no counterpart (a run is one PRD); `RALPH_DOC_FILES` became `doc_files`.

Then create the PRD and label its tickets:

```bash
# tickets are sub-issues of the PRD; use GitHub's native "blocked by" for ordering
gh issue edit 12 --add-label ralph:queued
gh issue edit 13 --add-label ralph:queued,ralph:hitl-arch   # sensitive: never auto-merged
```

## Usage

```bash
cd /path/to/your-repo

ralph-gh run --prd 52                              # default: respect-hitl-arch, parallel from the config
ralph-gh run --prd 52 --autonomy halt-each-pr      # conservative: review, never merge
ralph-gh run --prd 52 --autonomy yolo --parallel 2 # auto-merge within the allowlist, two at a time
ralph-gh stop                                      # ask the running conductor of this repo to drain and stop
```

Run it from anywhere inside the repo. For overnight runs wrap it in tmux (it is not a daemon):

```bash
tmux new -s ralph 'cd /path/to/repo && caffeinate -dims ralph-gh run --prd 52'
```

Exit codes: **0** every ticket was integrated and the final PR was merged or withheld by the autonomy mode, or the operator stopped the run; **1** startup or config error (bad config, failed preflight, another conductor already running); **2** CLI usage error; **3** the run ended incomplete (a failed or blocked ticket, cascade, usage limit, head moved after review, review rounds exhausted); **129** SIGHUP. The last line printed says why.

## Autonomy modes

The mode only governs the **final PR**: ticket PRs are always merged by the conductor into the integration branch, after their ticket gate PASSes. Whatever the mode, the final review must PASS first and the merge is pinned to the reviewed commit.

| Mode | Behavior |
|---|---|
| `halt-each-pr` | Never merges the final PR; the review verdict is posted, the PR is marked ready, merge is yours. |
| `respect-hitl-arch` (default) | Merges the final PR on PASS, unless the PRD or any ticket has `ralph:hitl-arch` (then it is withheld for a human). |
| `yolo` | Like `respect-hitl-arch`, and additionally merges only if **every** changed file of the final diff matches `yolo_allowlist`. An empty allowlist, an invalid regex or an unreadable diff withholds the merge (fail closed). Allowlist entries are Python regexes matched with `re.search` (unanchored), so anchor them with `^`. |

A withheld merge labels the PRD `ralph:gate-passed`, comments why on the PR, and the run exits 0. If the head of the final PR moved between the review and the merge, nothing is merged: the PR stays open and the run exits 3 (`head moved after review`); rerun to review it again in full.

## Label semantics

| Label | On | Meaning |
|---|---|---|
| `ralph:queued` | ticket | Ready to work (manual: this is what you apply to start) |
| `ralph:in-progress` | ticket | A session is implementing it |
| `ralph:in-review` | ticket | PR open into the integration branch, awaiting verify/gate/merge |
| `ralph:integrated` | ticket | Merged into the integration branch |
| `ralph:done` | PRD, tickets | The final PR merged; the issue is closed |
| `ralph:gate-passed` | PRD | Final review PASS, merge withheld for a human (autonomy mode) |
| `ralph:blocked` | ticket, PRD | A human decision is needed: a session reported `RALPH:BLOCKED`, or the final review could not be completed (rounds exhausted, fix blocked, verdict unparsable twice) |
| `ralph:failed:issue` | ticket | Per-ticket failure (verify or gate still failing after the fix rounds, unusable session outcome, a conflict the merge-fix sessions could not resolve); PR left open |
| `ralph:hitl-arch` | ticket, PRD | Manual: architecturally sensitive. Dispatched last; stops `respect-hitl-arch` and `yolo` from auto-merging the final PR |
| `ralph:failed:systemic` | ticket | Infrastructure failure, not the ticket's fault: a `git` or `gh` call failed during the ticket (push, PR creation, deleting a stale branch...), or a merge was refused for a reason other than a conflict. A diagnosis comment says which; the worktree is removed and independent tickets carry on. It halts dependents like `ralph:failed:issue`. A human fixes the cause and re-applies `ralph:queued` |

The conductor creates any missing `ralph:*` label at startup and never touches ones that exist. A ticket's labels are *re-read from GitHub* at the start of every run and reconciled against its PR (a human merge or close is honoured), failing closed: if a PR cannot be read the ticket is left exactly as it is.

`ralph:failed:*` is never applied for a session that died because the *account* hit a usage limit — see [Usage limits](#usage-limits).

## Parallelism and its usage cost

`parallel` (default 3, `--parallel N` per run) is how many tickets are worked at once, each with its own claude session and worktree. The conductor thread is the only one that writes to GitHub or git; worker threads only run sessions and verify commands, and merges into the integration branch are serial. A ticket is dispatched only when its dependencies are integrated, so a linear chain runs one at a time whatever `parallel` is.

**Parallel runs spend your Claude usage roughly N times as fast.** A claude.ai subscription has a rolling session window and a weekly cap shared by every session you run; three concurrent tickets consume that quota about three times faster than one, and a long implementer, plus its gates and fix rounds, is not cheap. A run is also not bounded by an iteration count any more: it ends when the PRD is done, blocked, or the quota is hit. Start with `--parallel 1` or `2` on a fresh quota, watch the first tickets, and raise it when you know what a ticket costs. Reviewer sessions run on Sonnet (ticket gate) and Opus (final review).

## Stop signals

Stopping is an operator action; there are two levels.

| Trigger | Level | Effect |
|---|---|---|
| `ralph-gh stop` (from another terminal), or creating the `STOP` file in the state directory; or a first `Ctrl-C` (SIGINT) | **Graceful** | Nothing new is dispatched; in-flight tickets finish normally (verify, gate, merge); no final review is started; the run exits `stopped by operator`, exit 0. |
| A second `Ctrl-C`, or `SIGTERM` | **Immediate** | Every running `claude` and verify process (each in its own process group) is killed. A ticket with no open PR goes back to `ralph:queued` with a comment; one with an open PR stays `ralph:in-review`. Exit `stopped by operator (immediate)`, exit 0. |
| `SIGHUP` (terminal closed) | Immediate | As above, exit 129. |

Stop state is set *before* any session is killed and before any label moves, so a late result is never acted on. A `STOP` file left over from an earlier run is removed at startup (after the run lock is held), so it can never block a new run. The next `ralph-gh run --prd N` resumes from the board: integrated tickets stay integrated, tickets left in review continue at verify/gate/merge. Running under the `/ralph-gh` skill there is no terminal, so only the graceful `STOP` file is available from inside that session.

Sessions end with a **marker line** the conductor parses: reviewers with `GATE:PASS` / `GATE:FAIL`, writing sessions with `RALPH:DONE` / `RALPH:BLOCKED <reason>`. Only the last five non-blank lines of the session's result are considered, whole-line only; both markers of one kind in that window, a missing or quoted marker, or a session that was killed or timed out are *unparsable*, which is never a PASS and never done.

## Usage limits

Claude.ai subscription plans (Pro, Max, Team, Enterprise) expose no API to check remaining quota in the rolling session/weekly window, so ralph-gh cannot predict whether a run will finish before hitting one — any "will this fit in my quota?" check would be a guess dressed up as a check, and this project doesn't promise what it can't enforce. What it *can* do is react once a session has already reported hitting the wall, instead of misfiling a perfectly good ticket as failed because the account ran dry mid-session.

Detection lives in the `claude` adapter (`conductor/claude_output.py`). It first checks the session's raw JSON `is_error` field — a client/transport-set flag the model's own reply can never influence — and only then looks for the CLI's documented usage-limit wording (`You've hit your session limit`, `...weekly limit`, `...Opus limit`, the bare `...limit`, plus the differently-worded usage-credits-exhausted variant) in its `errors`, `result` text, or stderr. Gating on `is_error` first is deliberate: without it, a session merely *discussing* usage limits could say the trigger phrase in an ordinary successful reply without any limit ever being hit. Deliberately NOT matched: wordings for a permanently-disabled seat or a group limit set to $0 — those are entitlement problems, not transient quota, and requeue-and-retry would spin forever. This wording is **not a stable API** — Anthropic can reword it without notice — so the match is best-effort: anything that doesn't match falls through to the ordinary "no parsable result" handling, never a guess.

On a match:

- Neither `ralph:failed:systemic` nor `ralph:failed:issue` is ever applied, and a usage-limited session is never retried as if it had failed.
- New dispatches stop. Each in-flight ticket is **parked**: back to `ralph:queued` when it has no open PR, left `ralph:in-review` when it has one (a comment says why). A hit during exploration or the final review pauses the run the same way.
- **Default** (`wait_for_reset = false`): the run exits with code 3 and the reason `usage limit — <reset description>` in `last-run.md` (or `reset time unknown (not stated in session output)` when the session did not say). Rerun after the reset: it resumes from the board.
- **`wait_for_reset = true`**: instead of exiting, the conductor sleeps `usage_wait_seconds` (default 1800, slept in short slices so a stop request is honoured during the wait) and resumes. This is a bounded wait, not a precise sleep-until-reset — the reset time, when a session states one at all, is prose in a format ralph-gh does not try to parse. If the limit has not lifted yet, the next attempt hits it again and waits again.

## State and observability

- **GitHub** is the source of truth: labels, PRs, native sub-issues and dependencies, and the comments below.
- **Local** state is outside your repo, per repo, in `<claude config dir>/ralph-gh/state/<owner>__<repo>/`: `lock`, `STOP`, `run.log` and `last-run.md`, plus per PRD `prd-<N>/` with `notes/exploration.md`, `worktrees/` (removed when a ticket ends) and `final/verdict-round-<k>.md`. Nothing is written into your working tree, so the tree stays clean for preflight.

**When a run seems stalled, start with the PRD's `## ralph-gh status` comment**: a single comment, edited in place (never a new one per event, so it never spams notifications), to which the conductor appends a timestamped line per transition — ticket dispatched, PR opened, in review, integrated, failed or blocked, every session starting and finishing with its duration, merges, run ended. While at least one session runs, a `heartbeat: session alive (<roles>)` line is added at most every 5 minutes, so "is it still running?" never needs re-deriving from timestamps. Reading the existing comment fails closed (a read error never creates a second comment); a failed write is logged and the run continues.

`run.log` gets every narrative line with an ISO 8601 timestamp, appended across runs and echoed to the terminal. `last-run.md` is rewritten on every update — so even a hard kill leaves a file ending in an exit reason — and holds the run's header, a per-ticket timing breakdown (implementer, verify, gate and fix seconds, summed over rounds) and the final `Exit reason`. The `/ralph-gh` skill shows it at the end of a run. The review verdicts are `## Gate verdict` comments on the PRs; a second conductor on the same repo is refused by the run lock (exit 1, naming the pid).

## Releases

Tags and changelogs are automated with [release-please](https://github.com/googleapis/release-please): every push to `main` updates a standing release PR built from the conventional-commit history, and merging it cuts a GitHub release, bumps `version.txt`, and appends to `CHANGELOG.md` — nothing to run by hand. `install.sh` copies `version.txt` next to the launcher (and removes a stale one when your clone has none), so an installed copy always says which cut it is. Because the final PR is squash-merged, each PRD lands on the base branch as one conventional commit, which is what release-please reads.

## Recommended companions

The loop shines with these (not bundled — install them yourself, from [Matt Pocock's skills](https://github.com/mattpocock/skills)):

- **`wayfinder`** — turns an idea into a mapped backlog of issues with acceptance criteria (perfect upstream of ralph-gh)
- **`tdd`** — the red-green-refactor discipline the implementer sessions follow
- **`code-review`** — the two-axis (Standards/Spec) review; the reviewer agent auto-detects it, or any derivative of it, and falls back to an inline version otherwise

## Security — read this before an unattended run

**Threat model first**: ralph-gh is designed for private repos, or repos where everyone with write access is trusted. Only sub-issues of the PRD you name, labelled `ralph:queued` (labelling requires triage access), enter the loop, and only same-repo PRs are ever looked at — but issue bodies and PR comments written by anyone still reach your sessions as text (sessions read their ticket and PRD themselves). If you run it on a public repo, never label a stranger's issue without rewriting it in your own words, and prefer short supervised runs over overnight ones.

Every session (exploration, implementer, fix, reviewer) runs with `--dangerously-skip-permissions` and your `gh` credentials: it can run any command your user can. Be clear about what is and is not enforced:

- **Deterministic (Python):** which tickets are worked (PRD sub-issues, dependencies honoured), verdict and outcome parsing (an unparsable verdict is never a PASS, a killed or timed-out session is never done or PASS), the autonomy decision and the yolo allowlist (fail closed on every unknown, an empty allowlist or an unreadable diff), every merge (the conductor, never a session; pinned with `--match-head-commit` to the reviewed commit, never retried blindly), the verify commands (re-run by the conductor itself, whatever a session claims), the label state machine, session timeouts, and the single-conductor lock.
- **Prompt-level only:** everything the session prompts tell a session not to do (no merging, no label edits, no force-push, no `--no-verify`, no dependency changes). A misbehaving or prompt-injected session is not technically prevented from ignoring these; the ticket gate and the final review exist to catch the *output* of such a session before it reaches your base branch, not to make the session itself safe.
- **Untrusted input:** issue bodies, PR comments and code under review are attacker-controlled text that flows into permissionless sessions. The conductor passes findings to fix sessions from its own captured output rather than trusting PR comments, session prompts carry pointers (ticket numbers, paths, a validated commit SHA) rather than issue text, and fix sessions are told to treat quoted text as data — but that is still an instruction, not a guarantee.
- **Config is data.** `.ralph-gh.toml` is parsed, never sourced or evaluated; branch names are built from `[a-z0-9-]` only; every subprocess other than your own commands takes an argv list. The two values that *do* run through a shell are `verify_commands` and `preflight_command`, which you author: anyone with write access to the repo can put shell in them, so treat changes to `.ralph-gh.toml` like code review.

Practical rules:

- for unattended runs, prefer a **devcontainer** or throwaway VM: that is the actual security boundary, not the prompts;
- be careful on **public repos**: anyone can open issues and comment on PRs, and that text reaches your sessions;
- keep protective **git hooks** in the repo (block force-push, protected branches, destructive `gh` operations);
- the conductor refuses to start on a dirty tree, never edits issue bodies, and leaves the final PR open (draft or ready) whenever a review cannot be completed.

## What it does NOT do

- Does NOT create issues or PRDs — bring your own backlog (see `wayfinder` / a PRD-to-issues flow)
- Does NOT edit issue bodies (only comments and labels)
- Does NOT stop your infra on exit
- Does NOT run as a daemon — use tmux/screen for overnight
- Does NOT delete merged ticket branches on GitHub

## License

MIT
