"""The run lock: one conductor per repo (critical path: guards the board).

The lock file lives in the per-repo state directory and holds the PID of the
holder. Ownership is an exclusive, non-blocking `flock` on that file, which
the OS drops when the holder dies, however it dies. So a file left behind by a
dead process is simply taken over (its recorded PID is only logged), and two
live conductors can never both win, with no check-then-act race.
The file is never deleted: unlinking it would let a third process lock a
fresh inode while the old one is still held.
"""
import fcntl
import logging
import os
from pathlib import Path
from typing import IO

log = logging.getLogger("conductor")


class RunLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[str] | None = None
        self.holder: str = ""  # PID text found in the file when the lock was refused

    def acquire(self) -> bool:
        """True if this process now holds the lock; False if another live one does."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            self.holder = handle.read().strip()
            handle.close()
            return False
        handle.seek(0)
        previous = handle.read().strip()
        if previous:
            log.info(f"replacing the lock left by dead process {previous}")
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            self._file.truncate()
            self._file.flush()
        finally:
            self._file.close()  # closing drops the flock
            self._file = None
