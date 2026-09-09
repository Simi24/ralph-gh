#!/usr/bin/env bash
# ralph-gh — GitHub-issue-driven Ralph loop
# Project-agnostic orchestrator: spawns fresh `claude --print` sessions in series,
# driven by a per-project `.ralph-gh.config` at the repo root.
#
# Usage (from the repo root):
#   ralph-gh.sh [--autonomy=MODE] [--max-iterations=N]
#
# Modes: halt-each-pr | respect-hitl-arch (default) | yolo
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_PROMPT="$SCRIPT_DIR/CLAUDE.md"
EXAMPLE_CONFIG="$SCRIPT_DIR/example.ralph-gh.config"

AUTONOMY="respect-hitl-arch"
MAX_ITERATIONS=20

for arg in "$@"; do
  case "$arg" in
    --autonomy=*) AUTONOMY="${arg#*=}" ;;
    --max-iterations=*) MAX_ITERATIONS="${arg#*=}" ;;
    -h|--help)
      grep -E '^# ' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

case "$AUTONOMY" in
  halt-each-pr|respect-hitl-arch|yolo) ;;
  *) echo "invalid --autonomy: $AUTONOMY" >&2; exit 2 ;;
esac

# Locate repo root (where .ralph-gh.config lives)
if ! REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "not inside a git repo" >&2; exit 1
fi
cd "$REPO_ROOT"

CONFIG_FILE="$REPO_ROOT/.ralph-gh.config"
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "missing $CONFIG_FILE" >&2
  echo "" >&2
  echo "Copy the template and fill it in:" >&2
  echo "  cp $EXAMPLE_CONFIG $CONFIG_FILE" >&2
  echo "  \$EDITOR $CONFIG_FILE" >&2
  exit 1
fi

# Defaults the config can override
RALPH_VERIFY_COMMANDS=()
RALPH_PREFLIGHT_CMD=""
RALPH_PREFLIGHT_HEALTH_URL=""
RALPH_PREFLIGHT_HEALTH_RETRIES=30
RALPH_YOLO_ALLOWLIST=""
RALPH_DOC_FILES=()
RALPH_BRANCH_PREFIX="ralph"
RALPH_DEFAULT_BASE_BRANCH="main"
RALPH_GATE_FIX_ROUNDS=2   # external-gate FAIL -> fix session -> re-gate, at most this many times

# shellcheck disable=SC1090
source "$CONFIG_FILE"

if [[ ${#RALPH_VERIFY_COMMANDS[@]} -eq 0 ]]; then
  echo "$CONFIG_FILE must define RALPH_VERIFY_COMMANDS (non-empty array)" >&2
  exit 1
fi

# Dependency checks
for cmd in claude gh jq; do
  if ! command -v "$cmd" >/dev/null; then echo "$cmd not found in PATH" >&2; exit 1; fi
done
if ! gh auth status >/dev/null 2>&1; then echo "gh not authenticated" >&2; exit 1; fi

STATE_DIR="$REPO_ROOT/.ralph-gh"
LOG_FILE="$STATE_DIR/run.log"
LAST_RUN="$STATE_DIR/last-run.md"
mkdir -p "$STATE_DIR"

# Add .ralph-gh/ to .gitignore if missing
if [[ -f "$REPO_ROOT/.gitignore" ]] && ! grep -qxF '.ralph-gh/' "$REPO_ROOT/.gitignore"; then
  printf '\n.ralph-gh/\n' >> "$REPO_ROOT/.gitignore"
  echo "added .ralph-gh/ to .gitignore"
fi

# Working tree must be clean (ignoring .ralph-gh/)
if [[ -n "$(git status --porcelain | grep -v '^?? .ralph-gh/' || true)" ]]; then
  echo "working tree not clean (ignoring .ralph-gh/). aborting." >&2
  git status --short >&2
  exit 1
fi

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "$RALPH_DEFAULT_BASE_BRANCH" ]]; then
  echo "must start from $RALPH_DEFAULT_BASE_BRANCH (currently $CURRENT_BRANCH)" >&2
  exit 1
fi

git fetch origin "$RALPH_DEFAULT_BASE_BRANCH" --quiet
LOCAL=$(git rev-parse "$RALPH_DEFAULT_BASE_BRANCH")
REMOTE=$(git rev-parse "origin/$RALPH_DEFAULT_BASE_BRANCH")
if [[ "$LOCAL" != "$REMOTE" ]]; then
  echo "$RALPH_DEFAULT_BASE_BRANCH is not up-to-date with origin. pull first." >&2
  exit 1
fi

# Ensure labels
ensure_label() {
  local name="$1" color="$2" desc="$3"
  gh label create "$name" --color "$color" --description "$desc" 2>/dev/null || true
}
ensure_label "ralph:queued"          "0E8A16" "ralph-gh: ready to work"
ensure_label "ralph:blocked"         "C5DEF5" "ralph-gh: deps unresolved"
ensure_label "ralph:in-progress"     "FBCA04" "ralph-gh: iteration active"
ensure_label "ralph:needs-review"    "1D76DB" "ralph-gh: PR open, awaiting gate/merge"
ensure_label "ralph:hitl-arch"       "FFA500" "ralph-gh: architecturally sensitive, never auto-merge"
ensure_label "ralph:gate-passed"     "0052CC" "ralph-gh: external gate PASS, merge withheld for a human"
ensure_label "ralph:done"            "5319E7" "ralph-gh: merged"
ensure_label "ralph:failed:systemic" "B60205" "ralph-gh: infra/tooling failure"
ensure_label "ralph:failed:issue"    "D93F0B" "ralph-gh: per-issue implementation failure"

# Preflight (project-specific, e.g. docker compose up)
if [[ -n "$RALPH_PREFLIGHT_CMD" ]]; then
  if [[ -n "$RALPH_PREFLIGHT_HEALTH_URL" ]] && curl -sf "$RALPH_PREFLIGHT_HEALTH_URL" >/dev/null 2>&1; then
    : # already healthy
  else
    echo "running preflight: $RALPH_PREFLIGHT_CMD"
    eval "$RALPH_PREFLIGHT_CMD" >> "$LOG_FILE" 2>&1 || true
    if [[ -n "$RALPH_PREFLIGHT_HEALTH_URL" ]]; then
      for _ in $(seq 1 "$RALPH_PREFLIGHT_HEALTH_RETRIES"); do
        curl -sf "$RALPH_PREFLIGHT_HEALTH_URL" >/dev/null 2>&1 && break
        sleep 1
      done
    fi
  fi
fi

SESSION_ID="ralph-$(date +%s)"

# --- label watcher -----------------------------------------------------------
# Iteration sessions sometimes skip the CLAIM label swap (protocol violation,
# but the work itself is fine). While a session runs, reconcile the board:
# if the repo sits on an issue branch whose issue is still ralph:queued,
# claim it on the session's behalf. Scoped to this orchestrator by design —
# no global hooks.
label_watcher() {
  while true; do
    sleep 45
    local br issue labels
    br=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null) || continue
    [[ "$br" =~ issue-([0-9]+) ]] || continue
    issue="${BASH_REMATCH[1]}"
    labels=$(gh issue view "$issue" --json labels --jq '[.labels[].name]|join(",")' 2>/dev/null) || continue
    if [[ "$labels" == *"ralph:queued"* && "$labels" != *"ralph:in-progress"* ]]; then
      gh issue edit "$issue" --remove-label "ralph:queued" --add-label "ralph:in-progress" >/dev/null 2>&1 || true
      gh issue comment "$issue" --body "🤖 ralph-gh lease acquired @ $(date -Iseconds) — session $SESSION_ID iter $ITERATION (auto-claimed by orchestrator watcher)" >/dev/null 2>&1 || true
      echo "[watcher] auto-claimed issue #$issue (label swap was skipped by the session)" | tee -a "$LOG_FILE"
    fi
  done
}

# --- external review gate (arbiter tier) --------------------------------------
# The iteration session runs its own advisory gate and fixes findings in-context,
# but it does NOT merge. Merging requires THIS deterministic pipeline:
#   external gate session (fresh context) -> PASS => orchestrator merges
#                                         -> FAIL => fix session -> re-gate
# capped at RALPH_GATE_FIX_ROUNDS, then ralph:failed:issue. The verdict that
# authorizes a merge is produced by a session the orchestrator controls,
# never by the session under review.
run_claude_step() {
  # $1 = input file, $2 = output file
  claude --dangerously-skip-permissions --print \
    --add-dir "$REPO_ROOT" \
    < "$1" > "$2" 2>&1 || true
}

run_external_gates() {
  local pr_list
  pr_list=$(gh pr list --state open --json number,headRefName \
    --jq ".[] | select(.headRefName | startswith(\"$RALPH_BRANCH_PREFIX/\")) | \"\(.number) \(.headRefName)\"" 2>/dev/null) || return 0
  [[ -z "$pr_list" ]] && return 0

  local pr branch issue labels can_merge withheld_reason round verdict
  while read -r pr branch; do
    [[ "$branch" =~ issue-([0-9]+) ]] || continue
    issue="${BASH_REMATCH[1]}"
    labels=$(gh issue view "$issue" --json labels --jq '[.labels[].name]|join(",")' 2>/dev/null || echo "")

    # Eligibility is decided by the state machine, not the branch prefix:
    # only PRs whose issue is ralph:needs-review are awaiting this gate.
    # Human PRs on similarly-named branches, withheld hitl-arch PRs
    # (ralph:gate-passed) and exhausted failures (ralph:failed:issue) are
    # all skipped instead of being re-processed every iteration.
    if [[ "$labels" != *"ralph:needs-review"* ]]; then
      echo "[gate] PR #$pr skipped (issue #$issue is not ralph:needs-review)" | tee -a "$LOG_FILE"
      continue
    fi

    # Autonomy decides whether a PASS may auto-merge; the gate itself always runs.
    can_merge=1; withheld_reason=""
    if [[ "$AUTONOMY" == "halt-each-pr" ]]; then
      can_merge=0; withheld_reason="autonomy=halt-each-pr"
    elif [[ "$AUTONOMY" == "respect-hitl-arch" && "$labels" == *"ralph:hitl-arch"* ]]; then
      can_merge=0; withheld_reason="issue is ralph:hitl-arch"
    elif [[ "$AUTONOMY" == "yolo" && -n "$RALPH_YOLO_ALLOWLIST" ]]; then
      if gh pr diff "$pr" --name-only | grep -Ev "$RALPH_YOLO_ALLOWLIST" | grep -q .; then
        can_merge=0; withheld_reason="diff outside yolo allowlist"
      fi
    fi

    round=0; verdict="FAIL"
    while true; do
      round=$((round + 1))
      local gate_in="$STATE_DIR/gate-pr$pr-round$round.input.md"
      local gate_out="$STATE_DIR/gate-pr$pr-round$round.output.txt"
      cat > "$gate_in" <<EOF
You are the EXTERNAL review gate (arbiter tier) of a ralph-gh loop. Repo: $REPO_ROOT. Evaluate PR #$pr (branch \`$branch\`, base \`$RALPH_DEFAULT_BASE_BRANCH\`) for issue #$issue.

1. Spawn the \`ralph-gate-reviewer\` agent (Agent tool, subagent_type: "ralph-gate-reviewer") with the issue number, branch, PR number and base branch. If the repo's AGENTS.md prescribes its own gate agent, spawn that one instead.
2. Post the agent's full verdict as a comment on PR #$pr under the header \`## Gate verdict\` with the suffix \`(external gate, session $SESSION_ID, round $round)\`.
3. On the LAST line of your output print exactly \`GATE:PASS\` or \`GATE:FAIL\` and nothing else.

You must NOT modify files, push, merge, or edit labels. You only review and comment.
EOF
      echo "[gate] PR #$pr issue #$issue — external gate round $round" | tee -a "$LOG_FILE"
      run_claude_step "$gate_in" "$gate_out"
      if tail -5 "$gate_out" | grep -q '^GATE:PASS$'; then verdict="PASS"; break; fi
      if ! tail -5 "$gate_out" | grep -q '^GATE:FAIL$'; then
        echo "[gate] PR #$pr — no parsable verdict (treating as FAIL)" | tee -a "$LOG_FILE"
      fi
      [[ $round -gt $RALPH_GATE_FIX_ROUNDS ]] && break

      local fix_in="$STATE_DIR/fix-pr$pr-round$round.input.md"
      local fix_out="$STATE_DIR/fix-pr$pr-round$round.output.txt"
      cat > "$fix_in" <<EOF
You are a FIX session of a ralph-gh loop. The external gate FAILED PR #$pr (branch \`$branch\`, issue #$issue) in repo $REPO_ROOT.

1. Read the latest \`## Gate verdict\` comment on the PR (\`gh pr view $pr --comments\`) and the issue (\`gh issue view $issue\`).
2. \`git fetch origin && git checkout $branch && git pull\`.
3. Fix ONLY the gate findings. Respect the repo's AGENTS.md/CLAUDE.md. Hard rules: no new dependencies, no Co-Authored-By footers, no --no-verify, no force-push, do not touch labels, do not merge.
4. Run the verify commands, in order — all must pass before pushing:
$(for c in "${RALPH_VERIFY_COMMANDS[@]}"; do echo "   - \`$c\`"; done)
5. Commit (conventional, atomic) and push the branch.

On the LAST line print exactly \`FIX:DONE\` if pushed, or \`FIX:BLOCKED <short reason>\` if you cannot fix.
EOF
      echo "[gate] PR #$pr — spawning fix session (round $round)" | tee -a "$LOG_FILE"
      run_claude_step "$fix_in" "$fix_out"
      if ! tail -5 "$fix_out" | grep -q '^FIX:DONE$'; then
        echo "[gate] PR #$pr — fix session blocked or failed" | tee -a "$LOG_FILE"
        break
      fi
    done

    if [[ "$verdict" == "PASS" && $can_merge -eq 1 ]]; then
      if gh pr merge "$pr" --squash --delete-branch 2>>"$LOG_FILE"; then
        gh issue edit "$issue" --remove-label "ralph:needs-review" --remove-label "ralph:in-progress" --add-label "ralph:done" >/dev/null 2>&1 || true
        git checkout "$RALPH_DEFAULT_BASE_BRANCH" --quiet 2>/dev/null || true
        git pull --ff-only origin "$RALPH_DEFAULT_BASE_BRANCH" --quiet 2>/dev/null || true
        git branch -d "$branch" >/dev/null 2>&1 || true
        echo "[gate] PR #$pr MERGED by orchestrator (gate PASS, round $round)" | tee -a "$LOG_FILE"
      else
        echo "[gate] PR #$pr — merge command failed, left open" | tee -a "$LOG_FILE"
      fi
    elif [[ "$verdict" == "PASS" ]]; then
      gh issue edit "$issue" --remove-label "ralph:needs-review" --add-label "ralph:gate-passed" >/dev/null 2>&1 || true
      gh pr comment "$pr" --body "External gate: **PASS** — merge withheld by orchestrator ($withheld_reason). A human decides." >/dev/null 2>&1 || true
      echo "[gate] PR #$pr gate PASS, merge withheld ($withheld_reason)" | tee -a "$LOG_FILE"
    else
      gh issue edit "$issue" --remove-label "ralph:needs-review" --remove-label "ralph:in-progress" --add-label "ralph:failed:issue" >/dev/null 2>&1 || true
      gh pr comment "$pr" --body "External gate: **FAIL** after $RALPH_GATE_FIX_ROUNDS fix round(s). Left open for a human. See the \`## Gate verdict\` comments." >/dev/null 2>&1 || true
      echo "[gate] PR #$pr FAILED the external gate after fix rounds — ralph:failed:issue" | tee -a "$LOG_FILE"
    fi
  done <<< "$pr_list"
}

{
  echo ""
  echo "================================================================="
  echo "ralph-gh session: $SESSION_ID"
  echo "started: $(date -Iseconds)"
  echo "autonomy=$AUTONOMY  max_iterations=$MAX_ITERATIONS"
  echo "repo=$REPO_ROOT"
  echo "================================================================="
} | tee -a "$LOG_FILE"

{
  echo "# ralph-gh run — $SESSION_ID"
  echo "Started: $(date -Iseconds)"
  echo "Repo: $REPO_ROOT"
  echo "Autonomy: $AUTONOMY"
  echo ""
} > "$LAST_RUN"

ITERATION=0
EXIT_REASON="max-iterations"
while [[ $ITERATION -lt $MAX_ITERATIONS ]]; do
  ITERATION=$((ITERATION + 1))
  echo ""
  echo "================================================================="
  echo " iteration $ITERATION / $MAX_ITERATIONS"
  echo "================================================================="

  ITER_INPUT="$STATE_DIR/iter-$ITERATION.input.md"
  ITER_OUTPUT="$STATE_DIR/iter-$ITERATION.output.txt"

  # Render verify commands and doc files into the prompt
  {
    cat "$CLAUDE_PROMPT"
    echo ""
    echo "---"
    echo "## Runtime parameters"
    echo "- autonomy: \`$AUTONOMY\`"
    echo "- iteration: \`$ITERATION\` of \`$MAX_ITERATIONS\`"
    echo "- session: \`$SESSION_ID\`"
    echo "- repo root: \`$REPO_ROOT\`"
    echo "- state dir: \`$STATE_DIR\`"
    echo "- base branch: \`$RALPH_DEFAULT_BASE_BRANCH\`"
    echo "- branch prefix: \`$RALPH_BRANCH_PREFIX\`"
    echo ""
    echo "### Verify commands (run in order; first non-zero exit halts the iteration)"
    for c in "${RALPH_VERIFY_COMMANDS[@]}"; do
      echo "- \`$c\`"
    done
    echo ""
    if [[ ${#RALPH_DOC_FILES[@]} -gt 0 ]]; then
      echo "### Doc files (update only if the diff changes the public API or introduces a new public concept)"
      for f in "${RALPH_DOC_FILES[@]}"; do
        echo "- \`$f\`"
      done
      echo ""
    fi
    if [[ -n "$RALPH_YOLO_ALLOWLIST" ]]; then
      echo "### Yolo allowlist (regex over changed file paths)"
      echo '```'
      echo "$RALPH_YOLO_ALLOWLIST"
      echo '```'
      echo ""
    fi
    if [[ -n "$RALPH_PREFLIGHT_HEALTH_URL" ]]; then
      echo "### Preflight infra health check"
      echo "- URL: \`$RALPH_PREFLIGHT_HEALTH_URL\` (already started by orchestrator; re-check before integration tests)"
      echo ""
    fi
  } > "$ITER_INPUT"

  label_watcher & WATCHER_PID=$!

  if ! claude --dangerously-skip-permissions --print \
        --add-dir "$REPO_ROOT" \
        < "$ITER_INPUT" \
        > "$ITER_OUTPUT" 2>&1; then
    echo "claude invocation exited non-zero (continuing — may have emitted a promise)" | tee -a "$LOG_FILE"
  fi

  kill "$WATCHER_PID" 2>/dev/null || true
  wait "$WATCHER_PID" 2>/dev/null || true

  tail -50 "$ITER_OUTPUT" | tee -a "$LOG_FILE"

  # Binding review gate + merge — deterministic, not skippable by the session.
  run_external_gates

  if grep -q "<promise>QUEUE_EMPTY</promise>"    "$ITER_OUTPUT"; then EXIT_REASON="queue-empty";       break; fi
  if grep -q "<promise>SYSTEMIC_FAIL</promise>"  "$ITER_OUTPUT"; then EXIT_REASON="systemic-failure";  break; fi
  if grep -q "<promise>CASCADE_FAIL</promise>"   "$ITER_OUTPUT"; then EXIT_REASON="cascade-fail";      break; fi
  if grep -q "<promise>HALT</promise>"           "$ITER_OUTPUT"; then EXIT_REASON="halt-each-pr";      break; fi

  git checkout "$RALPH_DEFAULT_BASE_BRANCH" --quiet 2>/dev/null || true
  sleep 2
done

{
  echo ""
  echo "## Final"
  echo "- Iterations run: $ITERATION"
  echo "- Exit reason: $EXIT_REASON"
  echo "- Ended: $(date -Iseconds)"
  echo ""
  echo "## Issues touched this session"
  gh issue list \
    --label "ralph:in-progress,ralph:needs-review,ralph:failed:issue,ralph:failed:systemic,ralph:done" \
    --state all --limit 50 \
    --json number,title,labels \
    --jq '.[] | "- #\(.number) \(.title) [\(.labels | map(.name) | join(","))]"' 2>/dev/null || true
} >> "$LAST_RUN"

echo ""
echo "ralph-gh exited: $EXIT_REASON (after $ITERATION iterations)"
echo "summary: $LAST_RUN"
