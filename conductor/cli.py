"""`ralph-gh run --prd N [--autonomy MODE] [--parallel N]` and `ralph-gh stop`.

Wiring only: every decision lives in the modules it builds. Exit codes follow
DECISIONS 12 (argparse gives 2 for a usage error).
"""
import argparse
import sys
from dataclasses import replace
from pathlib import Path

from conductor.claude_agents import ClaudeAgents
from conductor.config import AUTONOMY_MODES, Config, ConfigError
from conductor.drift import drift_warning, install_dir
from conductor.gh_forge import GhForge
from conductor.gh_runner import subprocess_runner
from conductor.git_adapter import GitCli
from conductor.host import HostEnvironment
from conductor.legacy_config import load_repo_config
from conductor.observer import Observer
from conductor.repo_context import RepoContext, RepoError, find_repo
from conductor.result import EXIT_STARTUP_ERROR
from conductor.run import run
from conductor.signals import install_signal_handlers
from conductor.state_dir import default_state_root
from conductor.stopping import STOP_FILE, StopState, write_stop_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ralph-gh", description="Drive a PRD's sub-issues to a merged PR.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="work a PRD through to its final PR")
    run_parser.add_argument("--prd", type=int, required=True, help="issue number of the PRD")
    run_parser.add_argument("--autonomy", choices=AUTONOMY_MODES, help="merge policy for the final PR (default respect-hitl-arch)")
    run_parser.add_argument("--parallel", type=int, help="tickets worked at once (default: parallel from .ralph-gh.toml)")
    commands.add_parser("stop", help="ask the running conductor of this repo to finish in-flight tickets and stop")
    return parser


def _fail(message: str) -> int:
    print(f"ralph-gh: {message}", file=sys.stderr)
    return EXIT_STARTUP_ERROR


def _run_config(repo: RepoContext, prd: int, autonomy: str | None, parallel: int | None) -> Config:
    config = load_repo_config(repo.root)
    if parallel is not None:
        config = replace(config, parallel=parallel)
    return config.with_run(prd=prd, repo_root=repo.root, state_root=default_state_root(repo.name), autonomy=autonomy)


def _stop(repo: RepoContext) -> int:
    state_root = default_state_root(repo.name)
    write_stop_file(state_root)
    print(f"ralph-gh: asked the conductor of {repo.name} to stop ({state_root / STOP_FILE})")
    return 0


def _run(repo: RepoContext, config: Config) -> int:
    warning = drift_warning(install_dir())
    if warning:  # advisory: never blocks the run
        print(f"ralph-gh: WARNING: {warning}", file=sys.stderr)
    config.state_root.mkdir(parents=True, exist_ok=True)
    stop = StopState(config.state_root / STOP_FILE)
    restore = install_signal_handlers(stop)
    try:
        forge = GhForge(repo.name, subprocess_runner(repo.root))
        observer = Observer(forge, prd=config.prd, state_root=config.state_root, repo_root=repo.root, echo=True)
        result = run(
            config, forge, ClaudeAgents(stop.sessions), GitCli(repo.root),
            env=HostEnvironment(repo.root, config.state_root / "run.log"), observer=observer, stop=stop,
        )
    finally:
        restore()
    print(f"ralph-gh: {result.reason} (exit {result.exit_code})", file=sys.stderr)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run" and (args.prd < 1 or (args.parallel is not None and args.parallel < 1)):
        parser.error("--prd and --parallel must be positive integers")
    try:
        repo = find_repo(Path.cwd())
        if args.command == "stop":
            return _stop(repo)
        config = _run_config(repo, args.prd, args.autonomy, args.parallel)
    except (RepoError, ConfigError, ValueError) as error:
        return _fail(str(error))
    return _run(repo, config)
