"""`ralph-gh run --prd N [--autonomy MODE] [--parallel N]` and `ralph-gh stop`.

Wiring only: every decision lives in the modules it builds. Exit codes follow
DECISIONS 12 (argparse gives 2 for a usage error).
"""
import argparse
import sys
from dataclasses import replace
from pathlib import Path

from conductor.antigravity_agents import AntigravityAgents
from conductor.claude_agents import ClaudeAgents
from conductor.composite_agents import CompositeAgents
from conductor.config import AUTONOMY_MODES, ENGINES, Config, ConfigError
from conductor.gh_forge import GhForge
from conductor.gh_runner import subprocess_runner
from conductor.git_adapter import GitCli
from conductor.host import HostEnvironment
from conductor.legacy_config import load_repo_config
from conductor.legacy_install import legacy_install_notice
from conductor.observer import Observer
from conductor.ports import Agents
from conductor.repo_context import RepoContext, RepoError, find_repo
from conductor.result import EXIT_STARTUP_ERROR
from conductor.run import run
from conductor.signals import install_signal_handlers
from conductor.state_dir import claude_config_dir, default_state_root
from conductor.stopping import STOP_FILE, StopState, write_stop_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ralph-gh", description="Drive a PRD's sub-issues to a merged PR.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="work a PRD through to its final PR")
    run_parser.add_argument("--prd", type=int, required=True, help="issue number of the PRD")
    run_parser.add_argument("--autonomy", choices=AUTONOMY_MODES, help="merge policy for the final PR (default respect-hitl-arch)")
    run_parser.add_argument("--parallel", type=int, help="tickets worked at once (default: parallel from .ralph-gh.toml)")
    run_parser.add_argument("--engine", choices=ENGINES, help="agent engine for all roles (default claude)")
    run_parser.add_argument("--writer-engine", choices=ENGINES, help="engine for writing roles")
    run_parser.add_argument("--reviewer-engine", choices=ENGINES, help="engine for review roles")
    run_parser.add_argument("--writer-model", help="model for writing sessions")
    run_parser.add_argument("--reviewer-model", help="model for review sessions")
    commands.add_parser("stop", help="ask the running conductor of this repo to finish in-flight tickets and stop")
    return parser


def _fail(message: str) -> int:
    print(f"ralph-gh: {message}", file=sys.stderr)
    return EXIT_STARTUP_ERROR


def _run_config(
    repo: RepoContext,
    prd: int,
    autonomy: str | None,
    parallel: int | None,
    *,
    engine: str | None = None,
    writer_engine: str | None = None,
    reviewer_engine: str | None = None,
    writer_model: str | None = None,
    reviewer_model: str | None = None,
) -> Config:
    config = load_repo_config(repo.root)
    overrides: dict[str, object] = {}
    if parallel is not None:
        overrides["parallel"] = parallel
    if engine is not None:
        overrides["engine"] = engine
    if writer_engine is not None:
        overrides["writer_engine"] = writer_engine
    if reviewer_engine is not None:
        overrides["reviewer_engine"] = reviewer_engine
    if writer_model is not None:
        overrides["writer_model"] = writer_model
    if reviewer_model is not None:
        overrides["reviewer_model"] = reviewer_model
    if overrides:
        config = replace(config, **overrides)
    return config.with_run(prd=prd, repo_root=repo.root, state_root=default_state_root(repo.name), autonomy=autonomy)


def _stop(repo: RepoContext) -> int:
    state_root = default_state_root(repo.name)
    write_stop_file(state_root)
    print(f"ralph-gh: asked the conductor of {repo.name} to stop ({state_root / STOP_FILE})")
    return 0


def _build_agents(config: Config, stop: StopState) -> Agents:
    engines: dict[str, Agents] = {}
    needed = {config.resolved_writer_engine, config.resolved_reviewer_engine}
    if "claude" in needed:
        engines["claude"] = ClaudeAgents(stop.sessions, allow_manifest_edits=config.allow_manifest_edits)
    if "antigravity" in needed:
        engines["antigravity"] = AntigravityAgents(stop.sessions, allow_manifest_edits=config.allow_manifest_edits)

    return CompositeAgents(
        engines=engines,
        writer_engine=config.resolved_writer_engine,
        reviewer_engine=config.resolved_reviewer_engine,
        writer_model=config.resolved_writer_model,
        reviewer_model=config.resolved_reviewer_model,
    )


def _run(repo: RepoContext, config: Config) -> int:
    notice = legacy_install_notice(claude_config_dir())
    if notice:  # advisory: the legacy copy is never used and never blocks the run
        print(f"ralph-gh: WARNING: {notice}", file=sys.stderr)
    config.state_root.mkdir(parents=True, exist_ok=True)
    stop = StopState(config.state_root / STOP_FILE)
    restore = install_signal_handlers(stop)
    try:
        forge = GhForge(repo.name, subprocess_runner(repo.root))
        observer = Observer(forge, prd=config.prd, state_root=config.state_root, repo_root=repo.root, echo=True)
        agents = _build_agents(config, stop)
        result = run(
            config, forge, agents, GitCli(repo.root),
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
        config = _run_config(
            repo,
            args.prd,
            args.autonomy,
            args.parallel,
            engine=getattr(args, "engine", None),
            writer_engine=getattr(args, "writer_engine", None),
            reviewer_engine=getattr(args, "reviewer_engine", None),
            writer_model=getattr(args, "writer_model", None),
            reviewer_model=getattr(args, "reviewer_model", None),
        )
    except (RepoError, ConfigError, ValueError) as error:
        return _fail(str(error))
    return _run(repo, config)
