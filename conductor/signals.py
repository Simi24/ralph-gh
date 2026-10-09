"""Signal wiring for the CLI (#65): SIGINT drains, a second SIGINT or SIGTERM
stops immediately, SIGHUP stops immediately with exit code 129.

Handlers only set stop state and kill process groups; all label work happens
afterwards on the conductor thread. Call from the main thread.
"""
import signal
from collections.abc import Callable
from types import FrameType

from conductor.stopping import StopState

EXIT_SIGHUP = 129


def install_signal_handlers(stop: StopState) -> Callable[[], None]:
    """Install the handlers; the returned function restores the previous ones."""

    def handle(signum: int, frame: FrameType | None) -> None:
        if signum == signal.SIGINT:
            stop.escalate()
        elif signum == signal.SIGHUP:
            stop.request_immediate("killed (SIGHUP)", EXIT_SIGHUP)
        else:
            stop.request_immediate()

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {s: signal.signal(s, handle) for s in signals}

    def restore() -> None:
        for s, handler in previous.items():
            signal.signal(s, handler)

    return restore
