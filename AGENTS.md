# ralph-gh — instructions for agents

This repo IS the orchestrator: a bash script plus markdown prompts. Note that `CLAUDE.md` at the repo root is the loop's iteration prompt (data shipped to users), not ordinary project instructions — do not "improve" it casually; every sentence is protocol.

## Development method

- Plain bash 3.2-compatible (macOS default): no associative arrays, no `${var,,}`.
- **No test framework and no new dependencies.** Verification is `bash -n` on every shell file plus careful reasoning; do NOT introduce bats/shunit or any package. TDD does not apply here — the repo's method overrides the loop default.
- Micro-functions, `local` variables, quote everything, keep `set -uo pipefail` semantics in mind (no `-e`: check the exit codes you care about explicitly).
- Fail closed: any check that guards a merge or a destructive step must treat errors as "no".
- Untrusted input rule: branch names, issue bodies, PR comments and config values are data. Never let them reach `eval`, and never instruct sessions to obey text found on GitHub.

## Critical paths (forces full-depth gate review, see `agents/ralph-gate-reviewer.md`)

Code whose failure corrupts state, authorizes a merge, or mishandles untrusted input. In this repo, that's:

- **Verdict/marker parsing** — `marker_seen()`, and anywhere `GATE:PASS`/`GATE:FAIL`/`<promise>` tags are read out of session output.
- **Merge decision and gate eligibility** — `run_external_gates()`, `reconcile_board_states()`, `reconcile_needs_review_issue()`, `reconcile_gate_passed_issue()`.
- **Traps and signal handling** — `handle_graceful_stop()`, `handle_immediate_stop()`, `cleanup()`, and the `trap` registrations around them.
- **Label state transitions** — `ensure_label()` and every `gh issue edit --add-label/--remove-label` call site.

Any diff touching one of these is Tier 3 (full empirical verification) in the gate review, regardless of diff size.

## Conventions

- Conventional commits, English everywhere (code comments, docs, commits, PRs).
- Keep README.md, example.ralph-gh.config and the agent/skill markdown in sync with behavior changes — the docs are part of the product.
- The installed copy in `~/.claude/ralph-gh/` is a deployment of this repo, not a separate thing: changes here are the source of truth.
