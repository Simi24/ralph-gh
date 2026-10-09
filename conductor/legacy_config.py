"""A repo with only the old bash `.ralph-gh.config` is refused with the key mapping."""
from pathlib import Path

from conductor.config import CONFIG_FILE, Config, ConfigError, load_config

LEGACY_FILE = ".ralph-gh.config"

# (old variable, new key, syntax note)
MAPPING: tuple[tuple[str, str, str], ...] = (
    ("RALPH_VERIFY_COMMANDS", "verify_commands", "bash array -> TOML list of strings"),
    ("RALPH_PREFLIGHT_CMD", "preflight_command", "string"),
    ("RALPH_PREFLIGHT_HEALTH_URL", "preflight_health_url", "string"),
    ("RALPH_PREFLIGHT_HEALTH_RETRIES", "preflight_health_retries", "positive integer"),
    ("RALPH_YOLO_ALLOWLIST", "yolo_allowlist", "regex string"),
    ("RALPH_BRANCH_PREFIX", "branch_prefix", "string"),
    ("RALPH_DEFAULT_BASE_BRANCH", "base_branch", "string"),
    ("RALPH_GATE_FIX_ROUNDS", "gate_fix_rounds", "integer"),
    ("RALPH_GATE_AGENT", "reviewer_agent", "string"),
    ("RALPH_SESSION_TIMEOUT", "session_timeout", "integer, seconds"),
    ("RALPH_WAIT_FOR_RESET", "wait_for_reset", "0/1 -> false/true"),
    ("RALPH_USAGE_WAIT_SECONDS", "usage_wait_seconds", "positive integer"),
    ("RALPH_DOC_FILES", "doc_files", "bash array -> TOML list of paths"),
    ("export ANTHROPIC_MODEL=...", "model", "string"),
)


def legacy_message() -> str:
    lines = [
        f"{LEGACY_FILE} is no longer read; create {CONFIG_FILE} (TOML, plain data) with these keys:",
        *(f"  {old} -> {new}  ({note})" for old, new, note in MAPPING),
        "  new: allow_manifest_edits (true/false, default false: lets worker sessions edit dependency manifests)",
        "  new: parallel (integer >= 1, default 3)",
        "  dropped: --max-iterations (no counterpart: a run is one PRD)",
    ]
    return "\n".join(lines)


def load_repo_config(repo_root: Path) -> Config:
    path = repo_root / CONFIG_FILE
    if not path.exists() and (repo_root / LEGACY_FILE).exists():
        raise ConfigError(legacy_message())
    return load_config(path)
