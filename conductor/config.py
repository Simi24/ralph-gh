"""Config: `.ralph-gh.toml` read as data with tomllib. Nothing is executed at
load time (verify/preflight commands run later, through a shell, on purpose)."""
import re
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

CONFIG_FILE = ".ralph-gh.toml"
_PREFIX = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_AGENT = re.compile(r"^[A-Za-z0-9_-]+(:[A-Za-z0-9_-]+)?$")
_REF = re.compile(r"^[A-Za-z0-9._/-]+$")
_MODEL = re.compile(r"^[A-Za-z0-9._/:\[\]-]+$")  # model ids carry ':' and '[1m]'
AUTONOMY_MODES = ("halt-each-pr", "respect-hitl-arch", "yolo")
_RUN_FIELDS = {"prd", "repo_root", "state_root", "autonomy"}


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
    yolo_allowlist: tuple[str, ...] = ()  # Python regexes, re.search per changed path
    preflight_command: str = ""
    preflight_health_url: str = ""
    preflight_health_retries: int = 30
    wait_for_reset: bool = False  # after a usage limit: wait and resume instead of exiting
    usage_wait_seconds: int = 1800
    # Per-run values, supplied by the CLI (or by tests) through with_run()
    prd: int = 0
    repo_root: Path = Path(".")
    state_root: Path = Path(".")  # per-repo state dir: <root>/<owner>__<repo>
    autonomy: str = "respect-hitl-arch"  # CLI flag; applies to the final PR only

    def with_run(self, *, prd: int, repo_root: Path, state_root: Path, autonomy: str | None = None) -> "Config":
        config = replace(self, prd=prd, repo_root=repo_root, state_root=state_root)
        if autonomy is None:
            return config
        if autonomy not in AUTONOMY_MODES:
            raise ConfigError(f"autonomy: expected one of {', '.join(AUTONOMY_MODES)}")
        return replace(config, autonomy=autonomy)


_INT_KEYS = {"parallel", "gate_fix_rounds", "session_timeout", "preflight_health_retries", "usage_wait_seconds"}
_BOOL_KEYS = {"wait_for_reset"}


def _check_token(key: str, value: str, pattern: "re.Pattern[str]", what: str) -> None:
    """Config values reach git and claude argv lists: never an option, never `..`."""
    if not pattern.match(value) or value.startswith("-") or ".." in value:
        raise ConfigError(f"{key}: not a safe {what} (no leading '-', no '..', no spaces): {value!r}")


def _parse_allowlist(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
        raise ConfigError("yolo_allowlist: expected a list of non-empty regex strings")
    for pattern in value:
        try:
            re.compile(pattern)
        except re.error as e:
            raise ConfigError(f"yolo_allowlist: invalid regex {pattern!r}: {e}") from None
    return tuple(value)


def parse_config(data: dict[str, object]) -> Config:
    known = {f.name for f in fields(Config)} - _RUN_FIELDS
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown key(s): {', '.join(unknown)}")
    commands = data.get("verify_commands")
    if not isinstance(commands, list) or not commands or not all(isinstance(c, str) and c for c in commands):
        raise ConfigError("verify_commands: required non-empty list of strings")
    kwargs = {k: v for k, v in data.items() if k not in ("verify_commands", "yolo_allowlist")}
    allowlist = _parse_allowlist(data.get("yolo_allowlist", []))
    for key, value in kwargs.items():
        if key in _BOOL_KEYS:
            if not isinstance(value, bool):
                raise ConfigError(f"{key}: expected true or false")
        elif key in _INT_KEYS:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"{key}: expected a non-negative integer")
        elif not isinstance(value, str):
            raise ConfigError(f"{key}: expected a string")
    for key in ("parallel", "session_timeout", "preflight_health_retries", "usage_wait_seconds"):
        if kwargs.get(key, 1) < 1:  # type: ignore[operator]
            raise ConfigError(f"{key}: must be >= 1")
    _check_token("branch_prefix", str(kwargs.get("branch_prefix", "feat")), _REF, "ref")
    if not _PREFIX.match(str(kwargs.get("branch_prefix", "feat"))):
        raise ConfigError("branch_prefix: only [a-z0-9-]")
    _check_token("base_branch", str(kwargs.get("base_branch", "main")), _REF, "ref")
    for key in ("reviewer_agent", "ticket_gate_agent"):
        if key in kwargs:
            _check_token(key, str(kwargs[key]), _AGENT, "agent name")
    if "model" in kwargs:
        _check_token("model", str(kwargs["model"]), _MODEL, "model id")
    return Config(verify_commands=tuple(commands), yolo_allowlist=allowlist, **kwargs)  # type: ignore[arg-type]


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            return parse_config(tomllib.load(f))
    except FileNotFoundError:
        raise ConfigError(f"{path} not found") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
