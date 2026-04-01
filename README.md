# Whittler

Whittler is an orchestrator that polls a `bd` CLI tool for ready work units called "beads", claims them and creates git worktrees for isolated development, spawns Docker containers running Claude Code as a solver agent to complete the work, and then commits results, merges to the main branch, and closes the beads.
