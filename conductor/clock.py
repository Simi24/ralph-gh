"""Time source, injectable so tests control timestamps and the heartbeat."""
from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()


def stamp(clock: Clock) -> str:
    """ISO 8601 with offset, seconds precision (same as `date -Iseconds`)."""
    return clock.now().isoformat(timespec="seconds")
