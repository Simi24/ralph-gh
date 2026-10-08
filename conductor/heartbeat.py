"""Heartbeat: while at least one session runs, a status line at most every 5 minutes."""
import threading
from datetime import datetime, timedelta

from conductor.clock import Clock

INTERVAL = timedelta(minutes=5)
POLL_SECONDS = 30.0


class Heartbeat:
    def __init__(self, post, clock: Clock, interval: timedelta = INTERVAL) -> None:  # post: Callable[[str], None]
        self._post = post
        self._clock = clock
        self._interval = interval
        self._lock = threading.Lock()
        self._running: list[str] = []
        self._last: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def session_started(self, label: str) -> None:
        with self._lock:
            if not self._running:
                self._last = self._clock.now()  # the window starts with the first session
            self._running.append(label)

    def session_ended(self, label: str) -> None:
        with self._lock:
            if label in self._running:
                self._running.remove(label)

    def tick(self) -> bool:
        """Post a heartbeat if one is due. Returns whether it did."""
        with self._lock:
            now = self._clock.now()
            if not self._running or self._last is None or now - self._last < self._interval:
                return False
            self._last = now
            running = ", ".join(self._running)
        self._post(f"heartbeat: session alive ({running})")
        return True

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            self.tick()
