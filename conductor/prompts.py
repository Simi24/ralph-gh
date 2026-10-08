"""Session prompts: context pointers plus the rules that are not obvious from
the repo. Never the PRD or ticket text."""
from pathlib import Path

from conductor.config import Config


def notes_pointer(notes: Path | None) -> str:
    """One line pointing at the exploration notes; empty when there are none.
    Used by every implementer and fix prompt. Never the notes' content."""
    if notes is None:
        return ""
    return f"Read the shared exploration notes first: {notes}\n"


def exploration_prompt(config: Config, notes: Path) -> str:
    return f"""Exploration for PRD #{config.prd}, before any implementer starts.

Read the PRD and its tickets yourself: `gh issue view {config.prd}` and the
sub-issues API.
Map the code the tickets touch: the modules, seams, conventions and test
fixtures an implementer must reuse, and the traps to avoid.

Write your notes to `{notes}` (outside the repo; do not write inside the repo).
Keep them short and factual: file paths and what lives there.

Finish with exactly one last line, plain text:
RALPH:DONE
or
RALPH:BLOCKED <short reason>
"""


def implementer_prompt(config: Config, ticket: int, integration: str, notes: Path | None = None) -> str:
    verify = "\n".join(f"- `{c}`" for c in config.verify_commands)
    return f"""You are implementing ticket #{ticket} of PRD #{config.prd}.

Read them yourself: `gh issue view {ticket}` and `gh issue view {config.prd}`.
Your worktree is your current directory, based on branch `{integration}`.
{notes_pointer(notes)}
Build the ticket with the `tdd` skill, test-first, and commit your work.
Before reporting done, merge `origin/{integration}` into your branch.

Verify commands (the conductor re-runs them itself):
{verify}

Rules: no dependency-manifest changes unless an acceptance criterion asks for
one, no force-push, no `--no-verify`. The repo's AGENTS.md overrides your
default development method.

Finish with exactly one last line, plain text:
RALPH:DONE
or
RALPH:BLOCKED <short reason>
"""


def ticket_gate_prompt(config: Config, ticket: int, integration: str) -> str:
    return f"""Light ticket gate for ticket #{ticket} of PRD #{config.prd}.

Read the ticket with `gh issue view {ticket}`. Review the diff
`origin/{integration}...HEAD` against the ticket's acceptance criteria and
spec only. Do not review standards or design.

FAIL if and only if at least one acceptance criterion is not met.
Finish with exactly one last line, plain text:
GATE:PASS
or
GATE:FAIL
"""
