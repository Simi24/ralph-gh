"""Per-ticket phase durations (implementer, verify, gate, fix), summed over rounds."""
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

PHASES = ("implementer", "verify", "gate", "fix")


def phase_of_role(role: str) -> str:
    """Map a session role to a reported phase. Any role containing 'fix' is a fix."""
    if "fix" in role:
        return "fix"
    if "gate" in role or "review" in role:
        return "gate"
    return role


class Timings:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seconds: dict[int, dict[str, float]] = {}

    @contextmanager
    def measure(self, ticket: int, phase: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.add(ticket, phase, time.monotonic() - started)

    def add(self, ticket: int, phase: str, seconds: float) -> None:
        with self._lock:
            phases = self._seconds.setdefault(ticket, {})
            phases[phase] = phases.get(phase, 0.0) + seconds

    def snapshot(self) -> dict[int, dict[str, float]]:
        with self._lock:
            return {t: dict(p) for t, p in self._seconds.items()}
