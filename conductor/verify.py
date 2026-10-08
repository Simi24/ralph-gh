"""Run the operator-authored verify commands (the only shell execution besides
preflight) in a worktree; first non-zero exit stops."""
import subprocess
from pathlib import Path


def run_verify(commands: tuple[str, ...], cwd: Path) -> bool:
    for command in commands:
        if subprocess.run(command, shell=True, cwd=cwd).returncode != 0:
            return False
    return True
