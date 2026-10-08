"""One exploration session per PRD, before the first implementer.

The notes live in the state directory, outside the repo. Sessions get a
pointer to the file, never its content. A failed exploration never stops the
run: callers get None and implementers start without the pointer.
"""
import logging
import shutil
from pathlib import Path

from conductor.config import Config
from conductor.markers import OutcomeKind, outcome_of
from conductor.ports import Agents, SessionRequest
from conductor.prompts import exploration_prompt
from conductor.usage_limit import UsageLimitHit, describe

log = logging.getLogger("conductor")


def notes_path(config: Config) -> Path:
    return config.state_root / f"prd-{config.prd}" / "notes" / "exploration.md"


def _has_notes(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def ensure_notes(config: Config, agents: Agents) -> Path | None:
    """Path of the exploration notes, running the session only if they are missing."""
    path = notes_path(config)
    if _has_notes(path):
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    session = agents.start(
        SessionRequest(
            role="exploration",
            prompt=exploration_prompt(config, path),
            cwd=config.repo_root,
            model=config.model,
            timeout=config.session_timeout,
            add_dirs=(path.parent,),
        )
    )
    if session.usage_limit:  # a pause, not a failed exploration (#62)
        shutil.rmtree(path.parent, ignore_errors=True)
        raise UsageLimitHit(describe(session))
    outcome = outcome_of(session)
    if outcome.kind is OutcomeKind.DONE and _has_notes(path):
        return path

    reason = "no notes written" if outcome.kind is OutcomeKind.DONE else f"outcome {outcome.kind.name.lower()}"
    log.warning("exploration for PRD #%d failed (%s); implementers start without notes", config.prd, reason)
    shutil.rmtree(path.parent, ignore_errors=True)  # never reuse a partial file
    return None
