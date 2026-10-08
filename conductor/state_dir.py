"""Where a repo's run state lives (DECISIONS 5):
`<claude config dir>/ralph-gh/state/<owner>__<repo>/`, with the config dir taken
from `CLAUDE_CONFIG_DIR` and falling back to `~/.claude`."""
import os
import re
from collections.abc import Mapping
from pathlib import Path

_NWO = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")


def claude_config_dir(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else (home or Path.home()) / ".claude"


def default_state_root(repo: str, environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """`repo` is `owner/name`; anything else is refused (it becomes a path component)."""
    found = _NWO.match(repo)
    if not found or ".." in repo:
        raise ValueError(f"not an owner/name repository: {repo!r}")
    owner, name = found.groups()
    return claude_config_dir(environ, home) / "ralph-gh" / "state" / f"{owner}__{name}"
