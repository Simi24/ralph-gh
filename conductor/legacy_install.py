"""Detect an `install.sh` installation under the Claude config dir, and say what to remove.

The plugin replaces it. The conductor never runs anything from there: it only reports. The
`ralph-gh/state/` directory is run state that the plugin keeps using, so it is never listed.
"""
from pathlib import Path

# Relative to the Claude config dir: what the old installer deployed.
LEGACY_PATHS = (
    "ralph-gh/conductor",
    "ralph-gh/ralph-gh",
    "ralph-gh/README.md",
    "ralph-gh/example.ralph-gh.toml",
    "ralph-gh/version.txt",
    "ralph-gh/.installed",
    "skills/ralph-gh",
    "agents/ralph-gate-reviewer.md",
    "agents/ralph-ticket-gate.md",
)


def legacy_install_notice(config_dir: Path) -> str | None:
    """A message naming every legacy path present, or None when there is no legacy installation."""
    found = [config_dir / rel for rel in LEGACY_PATHS if (config_dir / rel).exists() or (config_dir / rel).is_symlink()]
    if not found:
        return None
    removals = "\n".join(f"  rm -rf '{path}'" for path in found)
    return (
        "a legacy install.sh installation is present and is NOT used: ralph-gh now runs from the Claude Code plugin.\n"
        "Remove the old copy (keep ralph-gh/state, it holds your run state):\n"
        f"{removals}"
    )
