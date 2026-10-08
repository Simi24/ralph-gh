"""Run the operator-authored verify commands (the only shell execution besides
preflight) in a worktree; first non-zero exit stops."""
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    output: str = ""  # combined stdout and stderr of the failing command


def check_verify(commands: tuple[str, ...], cwd: Path) -> VerifyResult:
    for command in commands:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, errors="replace"
        )
        if proc.returncode != 0:
            return VerifyResult(False, f"$ {command}\n{proc.stdout}{proc.stderr}")
    return VerifyResult(True)


def run_verify(commands: tuple[str, ...], cwd: Path) -> bool:
    return check_verify(commands, cwd).ok
