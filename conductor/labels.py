"""Ticket state labels. Only the conductor writes these."""
QUEUED = "ralph:queued"
IN_PROGRESS = "ralph:in-progress"
IN_REVIEW = "ralph:in-review"
INTEGRATED = "ralph:integrated"
FAILED_ISSUE = "ralph:failed:issue"
FAILED_SYSTEMIC = "ralph:failed:systemic"  # infrastructure failure (git/gh), not the ticket's fault
BLOCKED = "ralph:blocked"
GATE_PASSED = "ralph:gate-passed"  # PRD only: final review passed, merge withheld for a human
DONE = "ralph:done"  # on the base branch
HITL_ARCH = "ralph:hitl-arch"  # manual label, never written by the conductor
