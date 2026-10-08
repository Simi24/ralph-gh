"""Run outcome and exit codes (DECISIONS 12)."""
from dataclasses import dataclass

EXIT_OK = 0
EXIT_STARTUP_ERROR = 1
EXIT_INCOMPLETE = 3


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    reason: str
