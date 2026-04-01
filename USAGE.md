# Whittler Usage Guide

This guide covers how to use Whittler as a human operator, and how to interact with it as an AI agent.

---

## For Humans

### Setting Up

**1. Install Whittler**

```bash
pip install -e .
```

**2. Build the solver image**

The default solver image packages Claude Code into a Docker container:

```bash
docker build -t whittler-solver:latest docker/
```

Or bring your own — any image that respects the `/bead.json` + `/work` contract works.

**3. Configure**

```bash
cp whittler.yaml.example whittler.yaml
```

Edit `whittler.yaml`. Key settings:

```yaml
repo_root: /path/to/your/repo
max_lanes: 4                       # how many beads to work concurrently
container_image: whittler-solver:latest
validation_command: "pytest"       # run after each bead is solved
agent_timeout: 900                 # kill container after 15 min
max_retries: 3                     # failures before a bead is quarantined
shutdown_timeout: 60               # seconds to drain in-flight tasks on SIGTERM
```

**4. Export your API key**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**5. Run**

```bash
whittler run
```

Whittler will start polling for ready beads, logging to the console and to `whittler.log`.

---

### Day-to-Day Operations

#### Starting and stopping

```bash
whittler run                       # start (blocks; Ctrl+C for graceful shutdown)
whittler run --dry-run             # preview what would be claimed, no containers spawned
whittler run --lanes 1             # process one bead at a time
```

`Ctrl+C` (or `SIGTERM`) triggers graceful shutdown: no new beads are claimed, and in-flight tasks are given `shutdown_timeout` seconds to finish. Any tasks still running after that are cancelled and their beads are unclaimed immediately.

#### Checking status

```bash
whittler status
```

Shows in-flight beads (id, branch, state, time elapsed) and recently completed ones.

You can also inspect the raw state file:

```bash
cat .whittler-state.json | python3 -m json.tool
```

#### After a crash

```bash
whittler cleanup
```

Removes orphaned git worktrees and stopped Docker containers from a previous run. Also reads `.whittler-state.json` and unclams any beads in `Claimed`/`Solving`/`Merging` state whose worktrees no longer exist, returning them to the ready queue.

Whittler also runs this cleanup automatically on startup.

#### Deferred (quarantined) beads

When a bead fails `max_retries` times in a row (agent exit 1, timeout, or no file changes), Whittler moves it to `deferred` status instead of re-queuing it indefinitely. To requeue a deferred bead after fixing its design:

```bash
bd show <id>                           # inspect the bead and current design
tail -n 200 whittler.log | grep <id>   # find the failure logs
bd update <id> --design "..."          # rewrite the design field
bd update <id> --status open           # reset to open so Whittler picks it up again
```

#### Merge conflicts

When a merge conflict is detected, Whittler:
- Logs an error with the conflicting files
- Preserves the worktree at `.worktrees/bead-<id>`
- Leaves the bead in `claimed` state (so nothing else picks it up)

To resolve:

```bash
# See what was preserved
ls .worktrees/

# Inspect the branch
git log bead/<id>

# Resolve manually, then merge, then close
git checkout main
git merge bead/<id>
# ... resolve conflicts ...
git commit
bd close <id>

# Clean up
git worktree remove .worktrees/bead-<id>
git branch -d bead/<id>
```

#### Monitoring logs

```bash
tail -f whittler.log              # all activity (DEBUG level)
```

Structured lifecycle events are logged as `WHITTLER_EVENT <json>` lines. Extract them with:

```bash
grep WHITTLER_EVENT whittler.log | sed 's/.*WHITTLER_EVENT //' | jq .
```

Event types: `claimed`, `solving`, `merged`, `conflict`, `failed`, `quarantined`, `error`.

---

### Bringing Your Own Solver

You can replace the default solver container with any image. The only requirements:

- Read `/bead.json` for the task
- Write code to `/work/`
- Exit 0 on success, non-zero on failure
- Respect `WHITTLER_MAX_RETRIES` and `WHITTLER_VALIDATION_CMD` env vars

Example minimal solver (Python, using the Anthropic SDK):

```dockerfile
FROM python:3.12-slim
RUN pip install anthropic
COPY solve.py /solve.py
ENTRYPOINT ["python", "/solve.py"]
```

Then set `container_image: my-solver:latest` in your config.

---

### Tuning for Your Project

| Scenario | Suggested config |
|----------|-----------------|
| Fast CI (< 2 min tests) | `validation_command: "pytest"`, `max_retries: 3` |
| Long build (> 10 min) | `agent_timeout: 1800`, `max_lanes: 2` |
| Expensive API calls | `max_lanes: 1`, `poll_interval: 10` |
| Large repo | `container_memory: 8g`, `container_cpu: 4` |
| Slow shutdown OK | `shutdown_timeout: 300` (5 min drain) |

---

## For AI Agents

This section is for AI assistants operating alongside or on top of Whittler — including agents that feed beads into the system, monitor it, or interact with the outputs.

### What Whittler Does (AI Summary)

Whittler is a background daemon. You do not control it directly — it runs autonomously. Your role is to:

1. **Create well-specified beads** that Whittler's solver agent can execute without guidance
2. **Monitor outcomes** and respond when things go wrong
3. **Handle conflicts** that Whittler flags for human review

### Creating Beads That Whittler Can Solve

A bead that Whittler can solve autonomously needs four things:

| Field | Purpose | What to put here |
|-------|---------|-----------------|
| `title` | Short task summary | One sentence, imperative ("Add login endpoint") |
| `description` | Long-form context | Background, constraints, what NOT to do |
| `design` | Agent instructions (becomes CLAUDE.md) | Step-by-step implementation guidance, file structure, code patterns to follow |
| `notes` | Expected file manifest | Newline-separated list of files the agent should create or modify |
| `acceptance_criteria` | What "done" looks like | Verifiable conditions ("POST /login returns 200 with valid JWT") |

**The `design` field is the most important.** It becomes `CLAUDE.md` in the solver's working directory. Write it as if you are writing instructions for yourself:

```
# Task: Add login endpoint

Implement POST /api/login in src/auth/routes.py.

## Constraints
- Use the existing User model in src/models/user.py
- Hash passwords with bcrypt (already a dependency)
- Return a JWT using the sign_token() helper in src/auth/jwt.py

## Steps
1. Add the route handler to src/auth/routes.py
2. Write the request/response schema in src/auth/schemas.py
3. Add tests in tests/test_login.py

## Do Not
- Add a registration endpoint (separate bead)
- Change the User model schema
```

### Detecting Bead Outcomes

After a bead is processed, check `bd show <id>` or `.whittler-state.json`:

```python
import json

with open(".whittler-state.json") as f:
    state = json.load(f)

for bead_id, record in state.items():
    print(bead_id, record["outcome"])
    # outcomes: "merged", "conflict", "agent_failed", "timeout", "no_changes", "error"
```

Or use the beads MCP server / `bd show <id>` to inspect status.

Check `bd show <id>` for beads that may have been quarantined — their status will be `deferred` rather than `open` or `closed`.

### Handling Failures

| Outcome | Meaning | What to do |
|---------|---------|-----------|
| `merged` | Success | Nothing — bead is closed |
| `conflict` | Merge conflict | Inspect `.worktrees/bead-<id>`, resolve manually or rewrite the bead |
| `agent_failed` | Solver exited non-zero | Check `whittler.log` for container output; rewrite the design field |
| `timeout` | Container exceeded `agent_timeout` | Break the bead into smaller pieces |
| `no_changes` | Agent made no file changes | Design field may be unclear; check logs |
| `error` | Unexpected exception | Check `whittler.log`; bead was unclaimed automatically |

Beads that fail `max_retries` times are moved to `deferred` by Whittler. To requeue after fixing:

```bash
bd update <id> --design "..."          # rewrite the design field
bd update <id> --status open           # requeue
```

For `agent_failed` or `no_changes` (before quarantine), read the logs and update the bead's `design` field:

```bash
bd show <id>                           # inspect the bead
tail -n 200 whittler.log | grep <id>   # find container output
bd update <id> --design "..."          # rewrite the design field
bd update <id> --status open           # requeue
```

### Reading Structured Events

Whittler emits `WHITTLER_EVENT <json>` log lines at every lifecycle transition. These are useful for programmatic monitoring:

```python
import json, subprocess

events = []
log = open("whittler.log").read()
for line in log.splitlines():
    if "WHITTLER_EVENT" in line:
        _, _, payload = line.partition("WHITTLER_EVENT ")
        events.append(json.loads(payload))

# Find all quarantined beads
quarantined = [e for e in events if e["event"] == "quarantined"]
```

### Beads That Work Well vs. Beads That Fail

**Works well:**
- Single-responsibility: one module, one feature, one bug fix
- Self-contained: agent has everything it needs in the design field
- Verifiable: a concrete validation command can confirm success
- Small scope: a well-scoped bead runs in under 10 minutes

**Fails or produces conflicts:**
- Multiple beads touching the same file (use dependency ordering with `bd dep add`)
- Vague design field ("implement the auth system")
- No acceptance criteria
- Requires understanding long context outside `/work`

### Dependency Ordering

Whittler processes beads in parallel. If two beads touch the same files, use `bd dep add` to serialize them:

```bash
bd dep add bead-002 bead-001   # bead-002 won't become ready until bead-001 is closed
```

Whittler will naturally pick up `bead-002` after `bead-001` merges, since it only processes beads returned by `bd ready` (which excludes blocked beads).

### Monitoring Whittler Programmatically

```python
import subprocess, json

# Check what's running
result = subprocess.run(
    ["whittler", "status"],
    capture_output=True, text=True
)
print(result.stdout)

# Read raw state
with open(".whittler-state.json") as f:
    state = json.load(f)

in_flight = [r for r in state.values() if r["state"] not in ("Closed", "Failed")]
failed = [r for r in state.values() if r["outcome"] in ("conflict", "agent_failed", "timeout")]
deferred = []  # beads quarantined by Whittler — check bd show <id> for status=deferred
```

### When to Intervene

Whittler is autonomous — the right default is to leave it running. Intervene when:

- A bead shows `conflict` outcome → resolve the merge manually
- Multiple beads keep failing on the same file → add dependency ordering
- A bead has been quarantined (`deferred`) → review logs, rewrite the design field
- The log shows repeated `agent_failed` for the same bead → rewrite the design field
- Whittler hasn't picked up new beads → check `whittler status` and the lock file

To check if Whittler is running:

```bash
cat .whittler.lock   # exists if Whittler holds the lock
whittler status      # shows current state without starting a new instance
```
