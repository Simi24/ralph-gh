"""last-run.md: rewritten per run, always ends with the exit reason.

It is written at startup with a sentinel reason and rewritten on every
update, so even a hard kill leaves a file that ends with an exit reason.
"""
import os
from pathlib import Path

from conductor.clock import Clock, stamp
from conductor.timings import PHASES

SENTINEL_REASON = "interrupted (no exit reason recorded)"


class LastRun:
    def __init__(self, path: Path, clock: Clock, session: str, prd: int, repo: str) -> None:
        self._path = path
        self._clock = clock
        self._header = f"# ralph-gh run — {session}\nStarted: {stamp(clock)}\nRepo: {repo}\nPRD: #{prd}\n"
        self._ended: str | None = None

    def update(self, timings: dict[int, dict[str, float]], reason: str | None = None) -> None:
        if reason is not None:
            self._ended = stamp(self._clock)
        ended = f"Ended: {self._ended}\n" if self._ended else ""
        text = (
            self._header
            + "\n"
            + "".join(_ticket_section(t, p) for t, p in sorted(timings.items()))
            + f"## Final\n{ended}Exit reason: {reason or SENTINEL_REASON}\n"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._path)


def _ticket_section(ticket: int, phases: dict[str, float]) -> str:
    lines = [f"- {name}: {round(phases[name])}s" if name in phases else f"- {name}: none" for name in PHASES]
    return f"## Ticket #{ticket} timing\n" + "\n".join(lines) + "\n\n"
