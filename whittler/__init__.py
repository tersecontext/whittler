"""
Whittler: An orchestrator for managing distributed work using Claude Code agents.

Whittler polls a bd CLI tool for ready work units called "beads", claims them
and creates git worktrees for isolated development, spawns Docker containers
running Claude Code as a solver agent to complete the work, and then commits
results, merges to the main branch, and closes the beads.
"""

__version__ = "0.1.0"
