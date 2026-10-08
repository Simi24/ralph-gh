"""One best-effort attempt at a forge write.

The real forge already retries label and comment writes once, so callers must
not retry again. A failure is logged, never raised, and reported as False."""
import logging
from collections.abc import Callable

log = logging.getLogger("conductor")


def attempt(action: Callable[[], object]) -> bool:
    """Run `action` once; True if it did not raise."""
    try:
        action()
        return True
    except Exception as error:  # noqa: BLE001 - best effort by design
        log.warning("write failed: %s", error)
        return False
