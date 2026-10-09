"""Wait for GitHub to report a commit the conductor just pushed (critical path: guards the
review/merge pin).

Right after a push the forge can still report the previous PR head. Reviewing that head would
review stale code, so the caller waits, bounded, until the PR head IS the pushed commit. Any
doubt (a different head, an unreadable head, a timeout) is False: the caller must not review.
"""
from conductor.ports import Forge
from conductor.usage_limit import Sleep

ATTEMPTS = 7  # 7 reads with 6 pauses of 5 s: about 30 s in production
INTERVAL = 5.0


def wait_for_head(
    forge: Forge, number: int, expected: str, sleep: Sleep, attempts: int = ATTEMPTS, interval: float = INTERVAL
) -> bool:
    for attempt_no in range(attempts):
        try:
            if forge.pr_head_sha(number) == expected:
                return True
        except Exception:  # noqa: BLE001 - an unreadable head is not a match; retry, then fail closed
            pass
        if attempt_no < attempts - 1:
            sleep(interval)
    return False
