"""
Tests for whittler.cli — config resolution and argument parsing.
"""

import json
import os
import textwrap
from argparse import Namespace
from unittest import mock

import pytest

from whittler.cli import _resolve_config, _apply_cli_overrides, _build_parser
from whittler.core import WhittlerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs) -> Namespace:
    """Build a minimal Namespace that _resolve_config / _apply_cli_overrides expect."""
    defaults = dict(
        config=None,
        lanes=None,
        repo=None,
        poll_interval=None,
        image=None,
        timeout=None,
        max_retries=None,
        validation_cmd=None,
        log_file=None,
    )
    defaults.update(kwargs)
    return Namespace(**defaults)


# ---------------------------------------------------------------------------
# Test 1: defaults when nothing is configured
# ---------------------------------------------------------------------------

def test_config_resolution_defaults(tmp_path, monkeypatch):
    """No config file, no env vars, no CLI args → WhittlerConfig defaults."""
    monkeypatch.chdir(tmp_path)
    # Ensure no WHITTLER_* env vars are set
    for key in list(os.environ):
        if key.startswith("WHITTLER_"):
            monkeypatch.delenv(key)

    args = _make_args()
    config = _resolve_config(args)

    expected = WhittlerConfig()
    assert config.max_lanes == expected.max_lanes
    assert config.repo_root == expected.repo_root
    assert config.poll_interval == expected.poll_interval
    assert config.agent_timeout == expected.agent_timeout


# ---------------------------------------------------------------------------
# Test 2: config file is loaded
# ---------------------------------------------------------------------------

def test_config_resolution_from_file(tmp_path, monkeypatch):
    """A config file with max_lanes: 5 should set config.max_lanes == 5."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("WHITTLER_"):
            monkeypatch.delenv(key)

    cfg_file = tmp_path / "custom.yaml"
    cfg_file.write_text("max_lanes: 5\n")

    args = _make_args(config=str(cfg_file))
    config = _resolve_config(args)

    assert config.max_lanes == 5


# ---------------------------------------------------------------------------
# Test 3: CLI overrides config file
# ---------------------------------------------------------------------------

def test_config_resolution_cli_overrides_file(tmp_path, monkeypatch):
    """yaml max_lanes: 5, --lanes 3 → config.max_lanes == 3."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("WHITTLER_"):
            monkeypatch.delenv(key)

    cfg_file = tmp_path / "w.yaml"
    cfg_file.write_text("max_lanes: 5\n")

    args = _make_args(config=str(cfg_file), lanes=3)
    config = _resolve_config(args)

    assert config.max_lanes == 3


# ---------------------------------------------------------------------------
# Test 4: env var overrides config file
# ---------------------------------------------------------------------------

def test_config_resolution_env_overrides_file(tmp_path, monkeypatch):
    """yaml max_lanes: 5, WHITTLER_MAX_LANES=7 → config.max_lanes == 7."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("WHITTLER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("WHITTLER_MAX_LANES", "7")

    cfg_file = tmp_path / "w.yaml"
    cfg_file.write_text("max_lanes: 5\n")

    args = _make_args(config=str(cfg_file))
    config = _resolve_config(args)

    assert config.max_lanes == 7


# ---------------------------------------------------------------------------
# Test 5: CLI overrides env var
# ---------------------------------------------------------------------------

def test_config_resolution_cli_overrides_env(tmp_path, monkeypatch):
    """env WHITTLER_MAX_LANES=7, --lanes 3 → config.max_lanes == 3."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("WHITTLER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("WHITTLER_MAX_LANES", "7")

    args = _make_args(lanes=3)
    config = _resolve_config(args)

    assert config.max_lanes == 3


# ---------------------------------------------------------------------------
# Test 6: status command with a state file
# ---------------------------------------------------------------------------

def test_status_command_with_state_file(tmp_path, monkeypatch, capsys):
    """status should print bead info when .whittler-state.json exists."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("WHITTLER_"):
            monkeypatch.delenv(key)

    state = {
        "bead-42": {
            "config": {
                "id": "bead-42",
                "description": "Fix the thing",
                "design": "",
                "notes": "",
                "body": "",
                "acceptance_criteria": "",
            },
            "state": "solving",
            "branch": "bead/bead-42",
            "worktree_path": "/tmp/wt",
            "container_id": "abc123",
            "attempts": 1,
            "errors": [],
            "claimed_at": 1700000000.0,
            "completed_at": 0.0,
            "outcome": "",
        }
    }

    state_file = tmp_path / ".whittler-state.json"
    state_file.write_text(json.dumps(state))

    # Point state_file via a config file
    cfg_file = tmp_path / "w.yaml"
    cfg_file.write_text(f"state_file: {str(state_file)}\n")

    from whittler.cli import cmd_status
    args = _make_args(config=str(cfg_file))
    rc = cmd_status(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "bead-42" in captured.out
    assert "solving" in captured.out
    assert "In-flight" in captured.out


# ---------------------------------------------------------------------------
# Test 7: argument parsing for `whittler run --lanes 4`
# ---------------------------------------------------------------------------

def test_argument_parsing_run():
    """Verify `whittler run --lanes 4` is parsed correctly."""
    parser = _build_parser()
    args = parser.parse_args(["run", "--lanes", "4"])

    assert args.command == "run"
    assert args.lanes == 4
    assert hasattr(args, "func")


# ---------------------------------------------------------------------------
# Test 8: env var same as default still overrides file
# ---------------------------------------------------------------------------

def test_env_var_same_as_default_still_overrides_file(tmp_path, monkeypatch):
    """yaml max_lanes: 5, WHITTLER_MAX_LANES=2 (the default) → config.max_lanes == 2.

    This tests that env vars override file values even when the env value equals
    the compiled default (i.e., when using direct os.environ inspection rather than
    comparing against a fresh WhittlerConfig()).
    """
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("WHITTLER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("WHITTLER_MAX_LANES", "2")

    cfg_file = tmp_path / "w.yaml"
    cfg_file.write_text("max_lanes: 5\n")

    args = _make_args(config=str(cfg_file))
    config = _resolve_config(args)

    assert config.max_lanes == 2
