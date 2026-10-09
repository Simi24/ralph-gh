"""The worker bundle: a plugin folder that only worker sessions load.

It holds the `ralph-guard` hooks and the ticket-gate and final-review agents. The conductor finds
it next to its own package (never through configuration) and starts every worker session with
`--plugin-dir <bundle>` and `RALPH_GUARD=1`. The guard stays inert in every other session.
"""
import os
from collections.abc import Mapping
from pathlib import Path

from conductor.ports import SessionRequest

BUNDLE_NAME = "ralph-guard"  # the bundle's plugin name: it namespaces the agents it provides
BUNDLE_DIRNAME = "worker-bundle"
ROOTS_ROLES = frozenset({"exploration"})  # the only role that writes outside its cwd (the notes dir)


def bundle_dir() -> Path:
    """The bundle inside the plugin this package is installed in (or the clone, when run from one)."""
    return Path(__file__).resolve().parent.parent / BUNDLE_DIRNAME


def guard_env(request: SessionRequest, allow_manifest_edits: bool, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """`base` (default: this process's environment) plus the guard's switches for one session."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if not k.startswith("RALPH_GUARD")}
    env["RALPH_GUARD"] = "1"
    env["RALPH_GUARD_ALLOW_MANIFESTS"] = "1" if allow_manifest_edits else "0"
    if request.role in ROOTS_ROLES:
        env["RALPH_GUARD_ROOTS"] = ":".join(str(d) for d in request.add_dirs)
    return env
