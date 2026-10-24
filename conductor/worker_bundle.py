"""The worker bundle: a plugin folder that only worker sessions load.

It holds the `ralph-guard` hooks and the ticket-gate and final-review agents. The conductor finds
it next to its own package (never through configuration) and starts every worker session with
`--plugin-dir <bundle>` and `RALPH_GUARD=1`. The guard stays inert in every other session.
"""
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from conductor.ports import SessionRequest

BUNDLE_NAME = "ralph-guard"  # the bundle's plugin name: it namespaces the agents it provides
BUNDLE_DIRNAME = "worker-bundle"
ROOTS_ROLES = frozenset({"exploration"})  # the only role that writes outside its cwd (the notes dir)

MANIFESTS = frozenset({
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "poetry.lock", "Pipfile", "Pipfile.lock", "setup.py", "setup.cfg",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
    "composer.json", "composer.lock",
})
REQUIREMENTS = re.compile(r"^requirements(?:[-.][\w.-]*)?\.txt$")


def bundle_dir() -> Path:
    """The bundle inside the plugin this package is installed in (or the clone, when run from one)."""
    return Path(__file__).resolve().parent.parent / BUNDLE_DIRNAME


def is_manifest_file(name: str) -> bool:
    return name in MANIFESTS or bool(REQUIREMENTS.match(name))


def check_manifest_edits(cwd: Path) -> str | None:
    """Check if any dependency manifest was modified or added in `cwd`."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                path = line[3:].strip()
                name = Path(path).name
                if is_manifest_file(name):
                    return name
    except Exception:
        pass
    return None


def guard_env(request: SessionRequest, allow_manifest_edits: bool, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """`base` (default: this process's environment) plus the guard's switches for one session."""
    raw = os.environ if base is None else base
    strip_keys = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_HOST"}
    env = {k: v for k, v in raw.items() if not k.startswith("RALPH_GUARD") and k not in strip_keys}
    env["RALPH_GUARD"] = "1"
    env["RALPH_GUARD_ALLOW_MANIFESTS"] = "1" if allow_manifest_edits else "0"
    if request.role in ROOTS_ROLES:
        env["RALPH_GUARD_ROOTS"] = ":".join(str(d) for d in request.add_dirs)

    # Prepend worker-bundle/bin to PATH so git/gh execution shims intercept writes
    bin_dir = str(bundle_dir() / "bin")
    current_path = env.get("PATH", "")
    env["PATH"] = f"{bin_dir}:{current_path}" if current_path else bin_dir

    # Point gh config to a nonexistent directory to ensure gh has no stored auth tokens
    env["GH_CONFIG_DIR"] = "/var/empty"

    # Block git push at git config level as defense-in-depth even if shim is bypassed
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "remote.origin.pushurl"
    env["GIT_CONFIG_VALUE_0"] = "http://127.0.0.1:0/blocked"
    env["GIT_SSH_COMMAND"] = "false"
    env["GIT_ASKPASS"] = "false"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env
