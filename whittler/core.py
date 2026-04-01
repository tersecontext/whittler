"""
Core data structures and types for Whittler.

This module defines the fundamental types and data structures used throughout
the Whittler orchestrator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields as dataclass_fields
from enum import Enum
from typing import Any

import yaml
from beads_mcp.models import Issue


@dataclass
class BeadConfig:
    """Thin adapter over beads_mcp.models.Issue with only the fields Whittler needs."""

    id: str
    description: str
    design: str
    notes: str

    @classmethod
    def from_issue(cls, issue: Issue) -> BeadConfig:
        """Create a BeadConfig from a beads_mcp Issue.

        Maps:
          - description <- issue.title  (the short summary of the work)
          - design      <- issue.design (may be None → "")
          - notes       <- issue.notes  (may be None → "")
        """
        return cls(
            id=issue.id,
            description=issue.title,
            design=issue.design or "",
            notes=issue.notes or "",
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary for state persistence."""
        return {
            "id": self.id,
            "description": self.description,
            "design": self.design,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BeadConfig:
        """Deserialize from a plain dictionary."""
        return cls(
            id=d["id"],
            description=d["description"],
            design=d.get("design", ""),
            notes=d.get("notes", ""),
        )


class BeadState(Enum):
    """Lifecycle states for a single bead (work item)."""

    Ready = "ready"
    Claimed = "claimed"
    Solving = "solving"
    Merging = "merging"
    Closed = "closed"
    Failed = "failed"


@dataclass
class BeadRecord:
    """Tracks a single bead through its full lifecycle."""

    config: BeadConfig
    state: BeadState
    branch: str
    worktree_path: str
    container_id: str
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    claimed_at: float = 0.0
    completed_at: float = 0.0
    outcome: str = ""  # "merged", "conflict", "agent_failed", "timeout"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary for state persistence."""
        return {
            "config": self.config.to_dict(),
            "state": self.state.value,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "container_id": self.container_id,
            "attempts": self.attempts,
            "errors": list(self.errors),
            "claimed_at": self.claimed_at,
            "completed_at": self.completed_at,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BeadRecord:
        """Deserialize from a plain dictionary."""
        return cls(
            config=BeadConfig.from_dict(d["config"]),
            state=BeadState(d["state"]),
            branch=d["branch"],
            worktree_path=d["worktree_path"],
            container_id=d["container_id"],
            attempts=d.get("attempts", 0),
            errors=list(d.get("errors", [])),
            claimed_at=d.get("claimed_at", 0.0),
            completed_at=d.get("completed_at", 0.0),
            outcome=d.get("outcome", ""),
        )


@dataclass
class WhittlerConfig:
    """All runtime configuration for the Whittler orchestrator.

    Field names match YAML keys so from_file() works with a direct mapping.
    """

    repo_root: str = "."
    max_lanes: int = 2
    poll_interval: int = 5
    agent_timeout: int = 900
    max_retries: int = 3
    container_image: str = "whittler-solver:latest"
    container_memory: str = "4g"
    container_cpu: int = 2
    worktree_base: str = ".worktrees"
    validation_command: str = ""
    api_key_env: str = "ANTHROPIC_API_KEY"
    log_file: str = "whittler.log"
    state_file: str = ".whittler-state.json"
    lock_file: str = ".whittler.lock"

    @classmethod
    def from_file(cls, path: str) -> WhittlerConfig:
        """Load a WhittlerConfig from a YAML file.

        Unknown keys in the file are silently ignored so that the config file
        can carry comments or extra tooling metadata without breaking Whittler.
        """
        with open(path) as fh:
            data: dict[str, Any] = yaml.safe_load(fh) or {}

        # Only pass keys that are actual dataclass fields to avoid TypeError.
        valid_fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    @classmethod
    def from_env(cls) -> WhittlerConfig:
        """Build a WhittlerConfig from WHITTLER_* environment variables.

        Each field can be overridden by an env var of the form
        WHITTLER_<FIELD_NAME_UPPER>, e.g. WHITTLER_MAX_LANES=4.

        Type coercion uses the default value's type as the target type.
        """
        instance = cls()
        prefix = "WHITTLER_"

        for fobj in dataclass_fields(cls):
            env_key = prefix + fobj.name.upper()
            raw = os.environ.get(env_key)
            if raw is None:
                continue
            # Infer target type from default value; fall back to str.
            target_type = type(fobj.default) if fobj.default is not None else str
            try:
                setattr(instance, fobj.name, target_type(raw))
            except (ValueError, TypeError):
                setattr(instance, fobj.name, raw)

        return instance
