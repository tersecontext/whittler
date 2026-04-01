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
7. **Fail safely** — On exit 1 or timeout, Whittler unclams the bead so it can be retried. After `max_retries` consecutive failures the bead is moved to `deferred` instead of re-queued indefinitely

Multiple beads are processed concurrently up to `max_lanes`.

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ | `python --version` |
| Go toolchain | Required to install `bd` and `beads-mcp`. `go version` |
| [`bd` CLI + `beads-mcp`](https://github.com/steveyegge/beads) | `go install github.com/steveyegge/beads/cmd/bd@latest` |
| Docker daemon | Running with access to build images |
| Anthropic API key | Set as `ANTHROPIC_API_KEY` in environment |
| A `beads` project with open work items | Fracture creates these; see the [fracture](https://github.com/tersecontext/fracture) repo |

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

You also need the `beads-mcp` package from your local [beads](https://github.com/steveyegge/beads) installation:

```bash
# 1. Install beads (provides bd CLI and beads-mcp)
go install github.com/steveyegge/beads/cmd/bd@latest

# 2. Find your beads-mcp path (Go module cache)
BEADS_VERSION=v0.59.0
BEADS_MCP_PATH=$(go env GOPATH)/pkg/mod/github.com/steveyegge/beads@${BEADS_VERSION}/integrations/beads-mcp

# 3. Update pyproject.toml to point to that path, then install
pip install -e .
```

If `go env GOPATH` is empty, it defaults to `~/go`. Verify with `bd --version` after install.

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
whittler cleanup          Remove stale worktrees, orphaned containers, and unclaim orphaned beads
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
| `--max-retries N` | 3 | Failures before a bead is quarantined to `deferred` |
| `--validation-cmd CMD` | _(none)_ | Command to validate after solve |
| `--log-file PATH` | `whittler.log` | Log file location |
| `--dry-run` | off | Log what would happen; don't claim or spawn |
| `-v` | off | Verbose (DEBUG) console output |

## Configuration

All options can be set in `whittler.yaml`, as `WHITTLER_*` environment variables, or as CLI flags. Precedence: **CLI > env > config file > defaults**.

See [`whittler.yaml.example`](whittler.yaml.example) for the full reference with comments.

## Failure Handling

### Automatic quarantine

Beads that fail repeatedly (agent exit 1, timeout, or no changes made) are automatically quarantined after `max_retries` consecutive failures. Instead of looping forever, the bead's status is set to `deferred`. A human can then review the bead, update its design field, and reset it to `open`.

### Graceful shutdown

`Ctrl+C` (or `SIGTERM`) stops new beads from being claimed and waits for in-flight tasks to finish. If tasks have not finished within `shutdown_timeout` seconds (default 60), they are cancelled and their beads are unclaimed immediately.

### Structured log events

Every lifecycle transition emits a `WHITTLER_EVENT <json>` log line. These can be extracted with:

```bash
grep WHITTLER_EVENT whittler.log | sed 's/.*WHITTLER_EVENT //' | jq .
```

Event types: `claimed`, `solving`, `merged`, `conflict`, `failed`, `quarantined`, `error`.

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
| Exit non-0 | Signal failure. Whittler unclams and retries up to `max_retries`. |

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

Whittler writes `.whittler-state.json` after each state transition. On restart it reads this file and detects orphaned worktrees and containers from the previous run.

`whittler cleanup` can also be run manually after a crash. It removes stale worktrees, orphaned containers, and unclams any beads in `Claimed`/`Solving`/`Merging` state whose worktrees no longer exist — returning them to the ready queue.

Only one Whittler instance may run per repo at a time (enforced by a `.whittler.lock` file lock).

## What Whittler Does Not Do

- Decide what to build — beads/Fracture does that
- Decompose work — Fracture does that
- Understand the codebase — the solver agent does that
- Resolve merge conflicts — flags for human review, preserves the worktree
- Run inside containers — it spawns containers

## License

MIT
