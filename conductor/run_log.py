"""run.log: every narrative line stamped, appended across runs.

A logging handler on the "conductor" logger. A multi-line message gets the
same timestamp on every line.
"""
import logging
import sys
from pathlib import Path
from typing import TextIO

from conductor.clock import Clock, stamp

LOGGER_NAME = "conductor"
BANNER_WIDTH = 65


class StampedHandler(logging.Handler):
    def __init__(self, path: Path, clock: Clock, stream: TextIO | None = None) -> None:
        super().__init__(logging.INFO)
        self._path = path
        self._clock = clock
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            when = stamp(self._clock)
            text = "".join(f"{when} {line}\n" for line in record.getMessage().splitlines() or [""])
            with self._path.open("a", encoding="utf-8") as f:
                f.write(text)
            if self._stream is not None:
                self._stream.write(text)
                self._stream.flush()
        except Exception:  # logging must never break a run
            self.handleError(record)


class RunLog:
    """Attach on start, detach on finish."""

    def __init__(self, path: Path, clock: Clock, *, echo: bool = False) -> None:
        self._handler = StampedHandler(path, clock, sys.stdout if echo else None)
        self._logger = logging.getLogger(LOGGER_NAME)
        self._old_level = self._logger.level
        path.parent.mkdir(parents=True, exist_ok=True)

    def attach(self) -> None:
        self._logger.addHandler(self._handler)
        if self._logger.level in (logging.NOTSET,) or self._logger.level > logging.INFO:
            self._logger.setLevel(logging.INFO)

    def detach(self) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._old_level)

    def banner(self, *lines: str) -> None:
        bar = "=" * BANNER_WIDTH
        self._logger.info("\n".join(["", bar, *lines, bar]))
