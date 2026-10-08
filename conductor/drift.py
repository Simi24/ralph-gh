"""Install drift check: warn when the installed copy is behind the clone it came from.

`install.sh` stamps `<install dir>/.installed` with the clone path and commit.
At startup the conductor compares that commit with the clone's current HEAD.
Advisory only: every missing piece (no stamp, unknown source, clone gone, not
git) means no warning, and nothing here touches the network.
"""
import subprocess
from collections.abc import Callable
from pathlib import Path

STAMP_FILE = ".installed"


def install_dir() -> Path:
    """The directory this package is installed in (or the clone, when run from one)."""
    return Path(__file__).resolve().parent.parent


def current_sha(clone: Path) -> str:
    """HEAD of `clone`, or "" when it cannot be read."""
    try:
        proc = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _stamp(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line)


def drift_warning(directory: Path, sha_of: Callable[[Path], str] = current_sha) -> str | None:
    stamp = _stamp(directory / STAMP_FILE)
    source, installed = stamp.get("source_path", ""), stamp.get("source_sha", "")
    if not source or not installed or installed == "unknown" or not Path(source).is_dir():
        return None
    current = sha_of(Path(source))
    if not current or current == installed:
        return None
    return f"installed copy is behind your clone ({installed[:7]} -> {current[:7]}) — run install.sh"
