"""Config: `.ralph-gh.toml` read as data with tomllib. Nothing is executed at
load time (verify/preflight commands run later, through a shell, on purpose)."""
import re
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

CONFIG_FILE = ".ralph-gh.toml"
_PREFIX = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_AGENT = re.compile(r"^[A-Za-z0-9_-]+(:[A-Za-z0-9_-]+)?$")
_RUN_FIELDS = {"prd", "repo_root", "state_root"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    # Read from .ralph-gh.toml
    verify_commands: tuple[str, ...]
    base_branch: str = "main"
    branch_prefix: str = "feat"
    parallel: int = 3
    gate_fix_rounds: int = 2
    reviewer_agent: str = "ralph-gate-reviewer"
    ticket_gate_agent: str = "ralph-ticket-gate"
    session_timeout: int = 7200
    model: str | None = None
    preflight_command: str = ""
    preflight_health_url: str = ""
    preflight_health_retries: int = 30
    # Per-run values, supplied by the CLI (or by tests) through with_run()
    prd: int = 0
    repo_root: Path = Path(".")
    state_root: Path = Path(".")  # per-repo state dir: <root>/<owner>__<repo>

    def with_run(self, *, prd: int, repo_root: Path, state_root: Path) -> "Config":
        return replace(self, prd=prd, repo_root=repo_root, state_root=state_root)


_INT_KEYS = {"parallel", "gate_fix_rounds", "session_timeout", "preflight_health_retries"}


def parse_config(data: dict[str, object]) -> Config:
    known = {f.name for f in fields(Config)} - _RUN_FIELDS
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown key(s): {', '.join(unknown)}")
    commands = data.get("verify_commands")
    if not isinstance(commands, list) or not commands or not all(isinstance(c, str) and c for c in commands):
        raise ConfigError("verify_commands: required non-empty list of strings")
    kwargs = {k: v for k, v in data.items() if k != "verify_commands"}
    for key, value in kwargs.items():
        if key in _INT_KEYS:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"{key}: expected a non-negative integer")
        elif not isinstance(value, str):
            raise ConfigError(f"{key}: expected a string")
    for key in ("parallel", "session_timeout", "preflight_health_retries"):
        if kwargs.get(key, 1) < 1:  # type: ignore[operator]
            raise ConfigError(f"{key}: must be >= 1")
    if not _PREFIX.match(str(kwargs.get("branch_prefix", "feat"))):
        raise ConfigError("branch_prefix: only [a-z0-9-]")
    for key in ("reviewer_agent", "ticket_gate_agent"):
        if key in kwargs and not _AGENT.match(str(kwargs[key])):
            raise ConfigError(f"{key}: invalid agent name")
    return Config(verify_commands=tuple(commands), **kwargs)  # type: ignore[arg-type]


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            return parse_config(tomllib.load(f))
    except FileNotFoundError:
        raise ConfigError(f"{path} not found") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
