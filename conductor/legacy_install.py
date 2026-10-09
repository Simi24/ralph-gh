"""Detect a pre-plugin `install.sh` installation and say how to remove it.

Advisory and read-only: the conductor always runs from the plugin it was
launched from, never from these files. Run state under `ralph-gh/state` is not
part of a legacy install and is never listed for removal.
"""
import shlex
from pathlib import Path

INSTALL_COMMAND = "/plugin install ralph-gh --marketplace Simi24/ralph-gh"

# (path under the claude config dir, is a directory)
_LEGACY_PATHS = (
    ("ralph-gh/conductor", True),
    ("ralph-gh/ralph-gh", False),
    ("ralph-gh/.installed", False),
    ("ralph-gh/version.txt", False),
    ("agents/ralph-gate-reviewer.md", False),
    ("agents/ralph-ticket-gate.md", False),
    ("skills/ralph-gh", True),
)


def legacy_install_report(config_dir: Path) -> str | None:
    """Removal steps for every leftover of the old installer under `config_dir`, or None."""
    steps = []
    for relative, is_dir in _LEGACY_PATHS:
        path = config_dir / relative
        if path.is_dir() if is_dir else path.is_file():
            steps.append(f"  {'rm -rf' if is_dir else 'rm -f'} {shlex.quote(str(path))}")
    if not steps:
        return None
    return "\n".join(
        [
            "found a legacy install.sh installation; it is ignored and never run. Remove it:",
            *steps,
            f"then install the plugin once: {INSTALL_COMMAND}",
        ]
    )
