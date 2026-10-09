"""The single `## ralph-gh status` comment on the PRD, edited in place.

One line per transition: `- <iso> <text>`. Reading existing comments fails
closed (never creates a second comment on a read error); writes fail open.
Writes are serialized, so parallel sessions can post safely.
"""
import logging
import threading

from conductor.clock import Clock, stamp
from conductor.ports import CommentForge

HEADER = "## ralph-gh status"
MAX_BODY = 60000  # GitHub's limit is 65536 characters
log = logging.getLogger("conductor")


class StatusComment:
    def __init__(self, forge: CommentForge, prd: int, clock: Clock) -> None:
        self._forge = forge
        self._prd = prd
        self._clock = clock
        self._lock = threading.Lock()
        self._comment_id: int | None = None
        self._body = ""

    def post(self, line: str) -> None:
        with self._lock:
            entry = f"- {stamp(self._clock)} {line}"
            if self._comment_id is None and not self._locate():
                return
            self._write(entry)

    def _locate(self) -> bool:
        """Find the existing status comment; True if there is one or we may create one."""
        try:
            comments = self._forge.list_comments(self._prd)
        except Exception:
            log.info(f"[status] PRD #{self._prd} could not read existing status comments, skipping this update")
            return False
        mine = [c for c in comments if c.body.startswith(HEADER)]
        if mine:
            self._comment_id, self._body = mine[-1].id, mine[-1].body
        else:
            self._body = HEADER + "\n"
        return True

    def _write(self, entry: str) -> None:
        body = _trim(self._body.rstrip("\n") + ("\n" if self._comment_id else "\n\n") + entry)
        try:
            if self._comment_id is None:
                self._comment_id = self._forge.create_comment(self._prd, body)
            else:
                self._forge.update_comment(self._comment_id, body)
            self._body = body
        except Exception:
            log.info(f"[status] PRD #{self._prd} could not update the status comment, continuing")


def _trim(body: str) -> str:
    """Drop the oldest lines after the header until the body fits."""
    lines = body.split("\n")
    while len("\n".join(lines)) > MAX_BODY and len(lines) > 3:
        del lines[2]
    return "\n".join(lines)
