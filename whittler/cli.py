"""
Command-line interface for Whittler.

This module provides the main entry point and CLI commands for running
the Whittler orchestrator.
"""

import asyncio
import argparse
import dataclasses
import json
import logging
import os
import signal
import sys
from argparse import Namespace
from datetime import datetime

from whittler.core import WhittlerConfig
from whittler import beads
from whittler import git
from whittler.containers import ContainerManager
from whittler.orchestrator import Orchestrator


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(log_file: str, verbose: bool = False) -> None:
    """Configure console and file logging handlers.

    Console handler: INFO level (or DEBUG if verbose).
    File handler: DEBUG level, append mode.
    Format: %(asctime)s %(levelname)s %(name)s: %(message)s
    """
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    formatter = logging.Formatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    # File handler
    fh = logging.FileHandler(log_file, mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    root.addHandler(fh)


# ---------------------------------------------------------------------------
# Config resolution helpers
# ---------------------------------------------------------------------------

def _resolve_config(args: Namespace) -> WhittlerConfig:
    """Resolve configuration using the layered precedence:

    defaults < config file < env vars < CLI args
    """
    # 1. Start with defaults
    config = WhittlerConfig()

    # 2. Load from file if --config given or whittler.yaml exists
    config_path: str | None = getattr(args, "config", None)
    if config_path is None and os.path.exists("whittler.yaml"):
        config_path = "whittler.yaml"
    if config_path is not None:
        config = WhittlerConfig.from_file(config_path)

    # 3. Apply env var overrides — directly inspect os.environ to respect env < CLI precedence
    # even when the env value equals the compiled default.
    env_overrides = {}
    for f in dataclasses.fields(WhittlerConfig):
        env_key = f"WHITTLER_{f.name.upper()}"
        raw = os.environ.get(env_key)
        if raw is not None:
            target_type = type(f.default) if not isinstance(f.default, dataclasses.Field) else type(getattr(WhittlerConfig(), f.name))
            try:
                env_overrides[f.name] = target_type(raw)
            except (ValueError, TypeError) as e:
                raise ValueError(f"{env_key}={raw}: cannot convert to {target_type.__name__}") from None
    if env_overrides:
        config = dataclasses.replace(config, **env_overrides)

    # 4. Apply explicit CLI args
    config = _apply_cli_overrides(config, args)

    return config


def _apply_cli_overrides(config: WhittlerConfig, args: Namespace) -> WhittlerConfig:
    """Apply explicitly-provided CLI arguments to config via dataclasses.replace()."""
    # Mapping from CLI dest name -> WhittlerConfig field name
    cli_to_field = {
        "lanes": "max_lanes",
        "repo": "repo_root",
        "poll_interval": "poll_interval",
        "image": "container_image",
        "timeout": "agent_timeout",
        "max_retries": "max_retries",
        "validation_cmd": "validation_command",
        "log_file": "log_file",
    }

    overrides: dict = {}
    for cli_dest, field_name in cli_to_field.items():
        val = getattr(args, cli_dest, None)
        if val is not None:
            overrides[field_name] = val

    if overrides:
        config = dataclasses.replace(config, **overrides)
    return config


# ---------------------------------------------------------------------------
# Command: run
# ---------------------------------------------------------------------------

def cmd_run(args: Namespace) -> int:
    """Start the Whittler orchestrator main loop."""
    config = _resolve_config(args)

    _setup_logging(config.log_file, verbose=getattr(args, "verbose", False))

    dry_run: bool = getattr(args, "dry_run", False)
    if dry_run:
        logger.info("[dry-run] Configuration resolved: %s", config)
        logger.info("[dry-run] Would start Orchestrator — exiting without action.")
        return 0

    orchestrator = Orchestrator(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, orchestrator.handle_signal, sig)

    try:
        loop.run_until_complete(orchestrator.run())
    finally:
        loop.close()

    return 0


# ---------------------------------------------------------------------------
# Command: status
# ---------------------------------------------------------------------------

def cmd_status(args: Namespace) -> int:
    """Read .whittler-state.json and print current state."""
    config = _resolve_config(args)
    state_file = config.state_file

    if not os.path.exists(state_file):
        print(f"No state file found at {state_file!r}.")
        return 0

    try:
        with open(state_file) as fh:
            data: dict = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Error reading state file: {exc}", file=sys.stderr)
        return 1

    in_flight = []
    completed = []

    for bead_id, record in data.items():
        outcome = record.get("outcome", "")
        if outcome:
            completed.append(record)
        else:
            in_flight.append(record)

    # In-flight beads
    print("=== In-flight beads ===")
    if not in_flight:
        print("  (none)")
    else:
        for rec in in_flight:
            bead_id = rec.get("config", {}).get("id", "?")
            state = rec.get("state", "?")
            branch = rec.get("branch", "?")
            claimed_at = rec.get("claimed_at", 0.0)
            claimed_str = (
                datetime.fromtimestamp(claimed_at).strftime("%Y-%m-%d %H:%M:%S")
                if claimed_at
                else "N/A"
            )
            print(f"  [{bead_id}] state={state} branch={branch} claimed_at={claimed_str}")

    # Recent completions (sorted by completed_at)
    print("\n=== Recent completions ===")
    if not completed:
        print("  (none)")
    else:
        completed_sorted = sorted(completed, key=lambda r: r.get("completed_at", 0.0))
        for rec in completed_sorted:
            bead_id = rec.get("config", {}).get("id", "?")
            outcome = rec.get("outcome", "?")
            completed_at = rec.get("completed_at", 0.0)
            completed_str = (
                datetime.fromtimestamp(completed_at).strftime("%Y-%m-%d %H:%M:%S")
                if completed_at
                else "N/A"
            )
            print(f"  [{bead_id}] outcome={outcome} completed_at={completed_str}")

    # Count summary
    print(f"\nTotal: {len(data)} bead(s) — {len(in_flight)} in-flight, {len(completed)} completed")
    return 0


# ---------------------------------------------------------------------------
# Command: cleanup
# ---------------------------------------------------------------------------

def cmd_cleanup(args: Namespace) -> int:
    """Remove stale worktrees and orphan containers."""
    config = _resolve_config(args)

    async def _run_cleanup() -> tuple[list[str], list[str], list[str]]:
        cleaned_wt = await git.cleanup_stale_worktrees(config.repo_root, config.worktree_base)
        cm = ContainerManager(config)
        cleaned_containers = await cm.cleanup_orphans()

        # Unclaim beads whose worktrees no longer exist
        unclaimed_beads: list[str] = []
        ORPHAN_STATES = {"Claimed", "Solving", "Merging"}
        if os.path.exists(config.state_file):
            try:
                with open(config.state_file) as fh:
                    state_data = json.load(fh)
                for bead_id, record in state_data.items():
                    if record.get("state") not in ORPHAN_STATES:
                        continue
                    worktree_path = record.get("worktree_path", "")
                    if worktree_path and not os.path.exists(worktree_path):
                        try:
                            await beads.unclaim(bead_id, config.repo_root)
                            unclaimed_beads.append(bead_id)
                        except Exception as exc:
                            logger.warning("Could not unclaim bead %s: %s", bead_id, exc)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read state file for orphan bead cleanup: %s", exc)

        return cleaned_wt, cleaned_containers, unclaimed_beads

    cleaned_wt, cleaned_containers, unclaimed_beads = asyncio.run(_run_cleanup())

    if cleaned_wt:
        print(f"Removed {len(cleaned_wt)} stale worktree(s): {', '.join(cleaned_wt)}")
    else:
        print("No stale worktrees found.")

    if cleaned_containers:
        print(f"Removed {len(cleaned_containers)} orphan container(s): {', '.join(cleaned_containers)}")
    else:
        print("No orphan containers found.")

    if unclaimed_beads:
        print(f"Unclaimed {len(unclaimed_beads)} orphaned bead(s): {', '.join(unclaimed_beads)}")
    else:
        print("No orphaned beads to unclaim.")

    return 0


# ---------------------------------------------------------------------------
# Argument parser construction
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whittler",
        description="Whittler: an orchestrator for distributed agent work.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # ------------------------------------------------------------------
    # Shared config options (used by all sub-commands)
    # ------------------------------------------------------------------
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to whittler.yaml (default: whittler.yaml if it exists)",
    )

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------
    run_parser = subparsers.add_parser(
        "run",
        parents=[shared],
        help="Start the orchestrator main loop.",
    )
    run_parser.add_argument("--lanes", type=int, default=None, metavar="N", help="Override max_lanes")
    run_parser.add_argument("--repo", default=None, metavar="PATH", help="Override repo_root")
    run_parser.add_argument("--poll-interval", type=int, default=None, dest="poll_interval", metavar="N", help="Override poll_interval")
    run_parser.add_argument("--image", default=None, metavar="NAME", help="Override container_image")
    run_parser.add_argument("--timeout", type=int, default=None, metavar="N", help="Override agent_timeout")
    run_parser.add_argument("--max-retries", type=int, default=None, dest="max_retries", metavar="N", help="Override max_retries")
    run_parser.add_argument("--validation-cmd", default=None, dest="validation_cmd", metavar="CMD", help="Override validation_command")
    run_parser.add_argument("--log-file", default=None, dest="log_file", metavar="PATH", help="Override log_file")
    run_parser.add_argument("--dry-run", action="store_true", default=False, help="Log what would happen but don't claim/spawn")
    run_parser.add_argument("--verbose", "-v", action="store_true", default=False, help="Enable DEBUG-level console logging")
    run_parser.set_defaults(func=cmd_run)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    status_parser = subparsers.add_parser(
        "status",
        parents=[shared],
        help="Show current orchestrator state.",
    )
    status_parser.set_defaults(func=cmd_status)

    # ------------------------------------------------------------------
    # cleanup
    # ------------------------------------------------------------------
    cleanup_parser = subparsers.add_parser(
        "cleanup",
        parents=[shared],
        help="Remove stale worktrees and orphan containers.",
    )
    cleanup_parser.set_defaults(func=cmd_cleanup)

    return parser


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_cli() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    return args.func(args)


def main() -> None:
    """Main entry point for the whittler CLI."""
    sys.exit(run_cli())
