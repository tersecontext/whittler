"""
Tests for the core module.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch



import pytest
import yaml

from beads_mcp.models import Issue
from whittler.core import BeadConfig, BeadRecord, BeadState, WhittlerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_issue(**kwargs) -> Issue:
    """Return a minimal valid Issue, optionally overriding fields."""
    defaults = dict(
        id="ISS-42",
        title="Fix the thing",
        description="A longer description of the thing",
        design="## Design\nDo it this way",
        notes="Some notes",
        status="open",
        priority=2,
        issue_type="task",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return Issue(**defaults)


def _make_bead_config() -> BeadConfig:
    return BeadConfig(
        id="ISS-1",
        description="Do something",
        design="## Design",
        notes="Important note",
        body="Long-form description of the work.",
        acceptance_criteria="All tests pass.",
    )


def _make_bead_record(state: BeadState = BeadState.Ready) -> BeadRecord:
    return BeadRecord(
        config=_make_bead_config(),
        state=state,
        branch="bead/ISS-1",
        worktree_path="/tmp/worktrees/ISS-1",
        container_id="abc123",
        attempts=1,
        errors=["something went wrong"],
        claimed_at=1000.0,
        completed_at=2000.0,
        outcome="merged",
    )


# ---------------------------------------------------------------------------
# BeadConfig
# ---------------------------------------------------------------------------

class TestBeadConfig:
    def test_round_trip(self):
        """from_dict → to_dict should produce identical data."""
        original = {
            "id": "ISS-99",
            "description": "Implement X",
            "design": "Use approach Y",
            "notes": "Be careful about Z",
            "body": "Longer description of X.",
            "acceptance_criteria": "X works correctly.",
        }
        config = BeadConfig.from_dict(original)
        assert config.to_dict() == original

    def test_round_trip_missing_optional(self):
        """from_dict tolerates missing optional fields (they default to '')."""
        minimal = {"id": "ISS-1", "description": "A task"}
        config = BeadConfig.from_dict(minimal)
        assert config.id == "ISS-1"
        assert config.description == "A task"
        assert config.design == ""
        assert config.notes == ""
        assert config.body == ""
        assert config.acceptance_criteria == ""
        assert config.to_dict() == {
            "id": "ISS-1",
            "description": "A task",
            "design": "",
            "notes": "",
            "body": "",
            "acceptance_criteria": "",
        }

    def test_from_issue_maps_title_to_description(self):
        issue = _make_issue()
        config = BeadConfig.from_issue(issue)
        assert config.id == issue.id
        assert config.description == issue.title  # title → description

    def test_from_issue_maps_design(self):
        issue = _make_issue(design="## My Design")
        config = BeadConfig.from_issue(issue)
        assert config.design == "## My Design"

    def test_from_issue_maps_notes(self):
        issue = _make_issue(notes="Pay attention here")
        config = BeadConfig.from_issue(issue)
        assert config.notes == "Pay attention here"

    def test_from_issue_none_design_becomes_empty_string(self):
        issue = _make_issue(design=None)
        config = BeadConfig.from_issue(issue)
        assert config.design == ""

    def test_from_issue_none_notes_becomes_empty_string(self):
        issue = _make_issue(notes=None)
        config = BeadConfig.from_issue(issue)
        assert config.notes == ""

    def test_from_issue_maps_body_from_description(self):
        issue = _make_issue(description="Detailed prose about the work.")
        config = BeadConfig.from_issue(issue)
        assert config.body == "Detailed prose about the work."

    def test_from_issue_maps_acceptance_criteria(self):
        issue = _make_issue(acceptance_criteria="All unit tests pass.")
        config = BeadConfig.from_issue(issue)
        assert config.acceptance_criteria == "All unit tests pass."

    def test_from_issue_none_acceptance_criteria_becomes_empty_string(self):
        issue = _make_issue(acceptance_criteria=None)
        config = BeadConfig.from_issue(issue)
        assert config.acceptance_criteria == ""


# ---------------------------------------------------------------------------
# BeadState
# ---------------------------------------------------------------------------

class TestBeadState:
    def test_has_six_values(self):
        assert len(BeadState) == 6

    def test_all_expected_values_exist(self):
        expected = {"Ready", "Claimed", "Solving", "Merging", "Closed", "Failed"}
        actual = {member.name for member in BeadState}
        assert actual == expected

    def test_values_are_lowercase_strings(self):
        for member in BeadState:
            assert member.value == member.name.lower()


# ---------------------------------------------------------------------------
# BeadRecord
# ---------------------------------------------------------------------------

class TestBeadRecord:
    def test_round_trip(self):
        """to_dict → from_dict should restore an equivalent record."""
        record = _make_bead_record(state=BeadState.Solving)
        serialized = record.to_dict()
        restored = BeadRecord.from_dict(serialized)

        assert restored.config.id == record.config.id
        assert restored.config.description == record.config.description
        assert restored.config.design == record.config.design
        assert restored.config.notes == record.config.notes
        assert restored.state == record.state
        assert restored.branch == record.branch
        assert restored.worktree_path == record.worktree_path
        assert restored.container_id == record.container_id
        assert restored.attempts == record.attempts
        assert restored.errors == record.errors
        assert restored.claimed_at == record.claimed_at
        assert restored.completed_at == record.completed_at
        assert restored.outcome == record.outcome

    def test_state_serialized_as_value(self):
        record = _make_bead_record(state=BeadState.Failed)
        d = record.to_dict()
        assert d["state"] == "failed"

    def test_defaults_when_deserializing_minimal_dict(self):
        minimal = {
            "config": {"id": "X", "description": "Y", "design": "", "notes": ""},
            "state": "ready",
            "branch": "bead/X",
            "worktree_path": "/tmp/X",
            "container_id": "",
        }
        record = BeadRecord.from_dict(minimal)
        assert record.attempts == 0
        assert record.errors == []
        assert record.claimed_at == 0.0
        assert record.completed_at == 0.0
        assert record.outcome == ""

    def test_errors_list_is_independent_copy(self):
        """Mutating the serialized list should not affect the record."""
        record = _make_bead_record()
        d = record.to_dict()
        d["errors"].append("extra")
        restored = BeadRecord.from_dict(d)
        # The original record's errors should be unchanged
        assert len(record.errors) == 1


# ---------------------------------------------------------------------------
# WhittlerConfig
# ---------------------------------------------------------------------------

class TestWhittlerConfig:
    def test_defaults(self):
        cfg = WhittlerConfig()
        assert cfg.repo_root == "."
        assert cfg.max_lanes == 2
        assert cfg.poll_interval == 5
        assert cfg.agent_timeout == 900
        assert cfg.max_retries == 3
        assert cfg.shutdown_timeout == 60
        assert cfg.container_image == "whittler-solver:latest"
        assert cfg.container_memory == "4g"
        assert cfg.container_cpu == 2
        assert cfg.worktree_base == ".worktrees"
        assert cfg.validation_command == ""
        assert cfg.api_key_env == "ANTHROPIC_API_KEY"
        assert cfg.log_file == "whittler.log"
        assert cfg.state_file == ".whittler-state.json"
        assert cfg.lock_file == ".whittler.lock"

    def test_from_file_loads_values(self, tmp_path):
        data = {
            "repo_root": "/repo",
            "max_lanes": 4,
            "poll_interval": 10,
            "container_image": "custom:v1",
        }
        config_file = tmp_path / "whittler.yaml"
        config_file.write_text(yaml.dump(data))

        cfg = WhittlerConfig.from_file(str(config_file))
        assert cfg.repo_root == "/repo"
        assert cfg.max_lanes == 4
        assert cfg.poll_interval == 10
        assert cfg.container_image == "custom:v1"
        # unspecified fields keep their defaults
        assert cfg.agent_timeout == 900
        assert cfg.max_retries == 3

    def test_from_file_ignores_unknown_keys(self, tmp_path):
        data = {"max_lanes": 3, "unknown_key": "ignored"}
        config_file = tmp_path / "whittler.yaml"
        config_file.write_text(yaml.dump(data))

        cfg = WhittlerConfig.from_file(str(config_file))
        assert cfg.max_lanes == 3
        assert not hasattr(cfg, "unknown_key")

    def test_from_file_empty_yaml(self, tmp_path):
        config_file = tmp_path / "whittler.yaml"
        config_file.write_text("")

        cfg = WhittlerConfig.from_file(str(config_file))
        # All defaults should be intact
        assert cfg.max_lanes == 2

    def test_from_env_overrides(self):
        env_overrides = {
            "WHITTLER_MAX_LANES": "8",
            "WHITTLER_POLL_INTERVAL": "30",
            "WHITTLER_CONTAINER_IMAGE": "my-image:latest",
        }
        with patch.dict(os.environ, env_overrides, clear=False):
            cfg = WhittlerConfig.from_env()

        assert cfg.max_lanes == 8
        assert cfg.poll_interval == 30
        assert cfg.container_image == "my-image:latest"
        # unspecified fields keep their defaults
        assert cfg.agent_timeout == 900

    def test_from_env_no_overrides(self):
        """With no WHITTLER_* vars set, from_env() returns all defaults."""
        # Remove any WHITTLER_* vars that might be set in the environment.
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("WHITTLER_")}
        with patch.dict(os.environ, clean_env, clear=True):
            cfg = WhittlerConfig.from_env()
        assert cfg == WhittlerConfig()

    def test_from_env_int_coercion(self):
        with patch.dict(os.environ, {"WHITTLER_AGENT_TIMEOUT": "1800"}, clear=False):
            cfg = WhittlerConfig.from_env()
        assert cfg.agent_timeout == 1800
        assert isinstance(cfg.agent_timeout, int)

    def test_from_env_raises_on_bad_int(self):
        """from_env() raises ValueError when an env var cannot be coerced to the target type."""
        with patch.dict(os.environ, {"WHITTLER_MAX_LANES": "notanumber"}, clear=False):
            with pytest.raises(ValueError, match="WHITTLER_MAX_LANES=notanumber: cannot convert to int"):
                WhittlerConfig.from_env()
