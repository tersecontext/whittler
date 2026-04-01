# Whittler

Whittler is a parallel code-execution orchestrator. It polls a [beads](https://github.com/steveyegge/beads) issue tracker for ready work items, claims them, spawns isolated Docker containers running Claude Code as solver agents, and lands the results on `main` via git.

It sits at the end of the AI pipeline:

```
user request → examine → fracture → beads → whittler → working code on main
```

## How It Works

1. **Poll** — `whittler run` calls `bd ready` on a loop, collecting unblocked beads
2. **Claim** — Each bead is atomically claimed before work begins
3. **Isolate** — A git worktree is created from `main` for each bead (`bead/<id>`)
4. **Solve** — A Docker container mounts the worktree at `/work`, reads `/bead.json`, writes `CLAUDE.md` from the bead's design field, and runs `claude -p` with the task prompt
5. **Validate** — The solver runs your configured `validation_command` (tests, lint, build); on failure it reads the errors, fixes them, and retries up to `max_retries` times
6. **Merge** — On exit 0, Whittler commits the worktree, acquires a merge lock, merges `--no-ff` to `main`, and closes the bead
7. **Fail safely** — On exit 1 or timeout, Whittler preserves the worktree for inspection and unclams the bead

Multiple beads are processed concurrently up to `max_lanes`.

## Requirements

- Python 3.11+
- Docker daemon running
- [beads / bd CLI](https://github.com/steveyegge/beads) installed
- Anthropic API key

## Installation

```bash
git clone https://github.com/tersecontext/whittler
cd whittler
pip install -e .
```

You also need the `beads-mcp` package from your local beads installation. The path in `pyproject.toml` points to the Go module cache by default — update it if your beads installation is elsewhere.

## Quick Start

```bash
# 1. Copy and edit the config
cp whittler.yaml.example whittler.yaml

# 2. Build or pull the solver image (or bring your own)
docker build -t whittler-solver:latest docker/

# 3. Set your API key
export ANTHROPIC_API_KEY=sk-ant-...

# 4. Run
whittler run
```

## Commands

```
whittler run [options]    Start the orchestrator loop
whittler status           Show in-flight and recently completed beads
whittler cleanup          Remove stale worktrees and orphaned containers
```

### `whittler run` options

| Flag | Default | Description |
|------|---------|-------------|
| `--config PATH` | `whittler.yaml` | Config file path |
| `--lanes N` | 2 | Max concurrent beads |
| `--repo PATH` | `.` | Git repo root |
| `--poll-interval N` | 5 | Seconds between polls |
| `--image NAME` | `whittler-solver:latest` | Solver container image |
| `--timeout N` | 900 | Agent timeout in seconds |
| `--max-retries N` | 3 | Max solver retries |
| `--validation-cmd CMD` | _(none)_ | Command to validate after solve |
| `--log-file PATH` | `whittler.log` | Log file location |
| `--dry-run` | off | Log what would happen; don't claim or spawn |
| `-v` | off | Verbose (DEBUG) console output |

## Configuration

All options can be set in `whittler.yaml`, as `WHITTLER_*` environment variables, or as CLI flags. Precedence: **CLI > env > config file > defaults**.

See [`whittler.yaml.example`](whittler.yaml.example) for the full reference with comments.

## Solver Container Contract

Whittler works with any container image that follows this contract:

| Item | Description |
|------|-------------|
| `/work` | Mounted read-write. This is the git worktree. Write code here. |
| `/bead.json` | Mounted read-only. Contains the bead config (see below). |
| `ANTHROPIC_API_KEY` | Env var. Set by Whittler from `api_key_env`. |
| `WHITTLER_MAX_RETRIES` | Env var. How many validation attempts to make. |
| `WHITTLER_VALIDATION_CMD` | Env var. Command to run to validate work. |
| Exit 0 | Signal success. Whittler commits and merges. |
| Exit non-0 | Signal failure. Whittler preserves worktree and unclames. |

`/bead.json` shape:

```json
{
  "id": "abc-123",
  "description": "Add a login endpoint",
  "body": "Long-form description of the task...",
  "design": "CLAUDE.md content — full agent instructions",
  "notes": "src/auth/login.py\ntests/test_login.py",
  "acceptance_criteria": "POST /login returns 200 with valid credentials"
}
```

The default solver image (`docker/`) runs Claude Code with `--dangerously-skip-permissions` and retries on validation failure.

## Crash Recovery

Whittler writes `.whittler-state.json` after each state transition. On restart it reads this file and detects orphaned worktrees and containers from the previous run. `whittler cleanup` can also be run manually after a crash.

Only one Whittler instance may run per repo at a time (enforced by a `.whittler.lock` file lock).

## What Whittler Does Not Do

- Decide what to build — beads/Fracture does that
- Decompose work — Fracture does that
- Understand the codebase — the solver agent does that
- Resolve merge conflicts — flags for human review, preserves the worktree
- Run inside containers — it spawns containers

## License

MIT
