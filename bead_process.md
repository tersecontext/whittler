# Bead Processing in Whittler

This document describes the full lifecycle of a bead from the moment Whittler sees it to the moment it is closed or flagged, and identifies the known gaps and improvement opportunities in the current implementation.

---

## The Lifecycle

### Overview

```
bd ready
    │
    ▼
claim ──────────────── fail → skip (no-op)
    │
    ▼
create worktree
    │
    ▼
save state
    │
    ▼
spawn container ─────── exception → kill + unclaim + remove worktree
    │
    ▼
wait for exit code
    │
    ├── exit 0 ──► commit
    │                  │
    │              no changes → unclaim + remove worktree
    │                  │
    │              changes committed
    │                  │
    │              acquire merge lock
    │                  │
    │              merge --no-ff to main
    │                  │
    │              ├── success → remove worktree + close bead + feedback
    │              └── conflict → preserve worktree (bead stays claimed)
    │
    ├── exit -1 ─► timeout → log + unclaim + remove worktree
    │
    └── exit 1+ ─► failure → log container output + unclaim + remove worktree
         │
    finally: cleanup container, save state
```

---

### Step-by-step

#### 1. Poll — `beads.ready()`

`orchestrator.run()` calls `bd ready --limit 10 --json` via `BdCliClient`. Returns issues with no blocking dependencies and status `open`. The poll runs on a configurable interval (`poll_interval`, default 5s). If no beads are ready, Whittler sleeps until the next poll or a shutdown signal.

**Concurrency:** All beads returned in a single poll are dispatched as concurrent asyncio tasks. The semaphore (`max_lanes`) gates entry — each task acquires the semaphore before doing any work.

---

#### 2. Claim — `beads.claim()`

Calls `bd update <id> --claim`. This sets the bead's status to `in_progress` atomically in the beads database.

If claim fails (e.g. another process claimed it first), the bead is skipped with outcome `claim_failed`. No worktree or container is created.

---

#### 3. Create worktree — `git.create_worktree()`

Creates:
- Branch `bead/<id>` from the current tip of `main`
- Git worktree at `<repo_root>/<worktree_base>/bead-<id>`

If a stale worktree or branch from a previous attempt exists, it is removed first. The worktree is a full copy of the repo at that branch — changes here are isolated from `main`.

---

#### 4. Save state

After the worktree is created, the `BeadRecord` is written to `.whittler-state.json`. This checkpoint enables crash recovery: if Whittler dies here, the next startup will find the in-progress record and clean up the orphaned worktree.

State is saved again at every significant transition: `Solving → Merging → Closed/Failed`.

---

#### 5. Spawn container — `ContainerManager.spawn()`

Writes the bead fields to a temp file and starts the Docker container:

| Mount / Env | Value |
|------------|-------|
| `/work` | `<worktree_path>` (read-write) |
| `/bead.json` | bead config JSON (read-only) |
| `ANTHROPIC_API_KEY` | from `api_key_env` in config |
| `WHITTLER_MAX_RETRIES` | `max_retries` from config |
| `WHITTLER_VALIDATION_CMD` | `validation_command` from config |

Container runs detached (`detach=True`, `auto_remove=False`). Resource limits: `mem_limit` and `nano_cpus` from config.

---

#### 6. Inside the container — `docker/entrypoint.sh`

The default solver:

1. Reads `/bead.json`
2. Writes the `design` field to `/work/CLAUDE.md`
3. Builds a prompt from `description`, `body`, `acceptance_criteria`, `notes`
4. Runs `claude -p "<prompt>" --dangerously-skip-permissions --max-turns 200`
5. Claude writes code to `/work`, then runs `WHITTLER_VALIDATION_CMD`
6. On validation failure: Claude reads the error, fixes, and reruns — up to `WHITTLER_MAX_RETRIES` times
7. Deletes `CLAUDE.md` (so it is not committed)
8. Exits 0 on success, 1 if retries exhausted

---

#### 7. Wait — `ContainerManager.wait()`

Blocks (in a thread executor) until the container exits, up to `agent_timeout` seconds.

- Returns the exit code on normal exit
- Returns `-1` and kills the container on timeout

---

#### 8. Commit, merge, or fail

**Exit 0:**
- `git add -A` + `git commit -m "bead(<id>): <description>"` in the worktree
- If nothing to commit → outcome `no_changes`, unclaim, remove worktree
- Acquire merge lock (serializes all merges)
- `git merge --no-ff bead/<id>` onto `main`
  - Success → remove worktree, `bd close <id>`, outcome `merged`
  - Conflict → **preserve worktree**, leave bead claimed, outcome `conflict`

**Exit -1 (timeout):**
- Log last 2000 chars of container output
- `bd update <id> --status open` (unclaim)
- Remove worktree
- Outcome: `timeout`

**Exit 1+ (agent failure):**
- Log last 2000 chars of container output
- Unclaim bead
- Remove worktree
- Outcome: `agent_failed`

**Unexpected exception:**
- Kill container (best-effort)
- Remove worktree (best-effort)
- Outcome: `error`

**Finally (always):**
- Remove container (Docker cleanup)
- Remove bead from in-flight state (except on `conflict`)
- Save state file

---

## Outcomes Reference

| Outcome | Bead state | Worktree | Container | Action needed |
|---------|-----------|----------|-----------|---------------|
| `merged` | closed | removed | removed | None |
| `conflict` | claimed | **preserved** | removed | Human resolves |
| `no_changes` | open | removed | removed | Rewrite design field |
| `agent_failed` | open | removed | removed | Check logs, rewrite design |
| `timeout` | open | removed | removed | Break bead into smaller pieces |
| `error` | open | removed | removed | Check logs for exception |
| `claim_failed` | open | never created | never created | None (another agent claimed it) |

---

## Known Gaps and Improvement Opportunities

### 1. Beads that fail indefinitely

**Current behaviour:** `agent_failed` and `timeout` beads are unclaimed and returned to `open`. They will appear in the next `bd ready` poll and be retried immediately, forever.

**Risk:** A bead with a bad design field, unsatisfiable acceptance criteria, or a dependency on missing infrastructure will loop until manually intervened.

**Improvements:**

- **Attempt counter in the bead itself.** The `BeadRecord.attempts` field exists but is never incremented. Increment it on each failure and persist it. After `max_retries` failures, call `bd update <id> --status deferred` or add a label (`failed:auto`) rather than unclaiming. A human then reviews deferred beads separately.

- **Exponential backoff.** Instead of immediate unclaim, add a delay before unclaiming proportional to `attempts`. This prevents a broken bead from monopolising a lane.

- **Poison bead detection.** Track failure counts in `.whittler-state.json` keyed by bead ID. If the same bead has failed N times across restarts, quarantine it instead of re-queuing.

Example sketch:
```python
record.attempts += 1
if record.attempts >= self.config.max_retries:
    # Quarantine: mark deferred instead of open
    await beads.update_status(bead.id, "deferred", self.config.repo_root)
    logger.warning("Bead %s quarantined after %d failures", bead.id, record.attempts)
else:
    await beads.unclaim(bead.id, self.config.repo_root)
```

---

### 2. Graceful shutdown

**Current behaviour:** `SIGINT`/`SIGTERM` sets `_shutdown`. The current batch of tasks finishes. No new beads are claimed. This is correct for the happy path.

**Gaps:**

- **No drain timeout.** If a container is running when shutdown is requested, Whittler waits indefinitely for it to finish. A solver stuck in an LLM call could block shutdown for the full `agent_timeout` (default 15 min).

- **No in-progress notification.** Beads that are `Solving` at shutdown time are left claimed. On restart they will be detected as orphans and cleaned up, but the bead stays claimed until then — invisible to other Whittler instances or humans checking `bd ready`.

**Improvements:**

- **Drain timeout.** After setting `_shutdown`, give in-progress tasks a grace period (e.g. 60s), then cancel them and kill their containers.

```python
def handle_signal(self, sig):
    self._shutdown.set()
    # Schedule forced shutdown after grace period
    asyncio.get_event_loop().call_later(60, self._force_shutdown)

def _force_shutdown(self):
    for task in self._active_tasks:
        task.cancel()
```

- **Unclaim on shutdown.** When a task is cancelled mid-flight, the `except CancelledError` path should attempt to unclaim the bead before re-raising, so it becomes available immediately rather than waiting for orphan cleanup on next start.

```python
except asyncio.CancelledError:
    logger.warning("Bead %s cancelled during shutdown", bead.id)
    await beads.unclaim(bead.id, self.config.repo_root)
    raise  # re-raise so the task actually cancels
```

- **State on SIGKILL.** A hard kill (`kill -9`) skips all cleanup. The state file will have orphaned records. The current startup cleanup handles worktrees and containers but does not unclaim beads — adding an unclaim step to `cleanup_stale_worktrees` recovery would help.

---

### 3. Error handling gaps

**Merge lock contention.** The merge lock serialises merges within one Whittler instance but provides no protection against a human doing `git checkout main && git merge ...` while Whittler is running. `merge_to_main` does call `verify_repo_health` before merging, but a race between the check and the merge is still possible.

**`bd` CLI unavailability.** All `beads.*` calls return safe defaults on `BdError`. This means if `bd` is down or misconfigured, Whittler silently polls an empty list forever rather than surfacing the failure. Adding a consecutive-error counter that logs a warning after N failed polls would make this visible.

**Docker daemon loss.** If Docker dies mid-run, `ContainerManager.spawn()` will raise and the bead will be unclaimed via the `except Exception` path. But the orphaned container (if Docker recovers) won't be cleaned up until next startup. Periodically calling `cleanup_orphans()` inside the main loop (not just on startup) would help.

**`_save_state` failure.** If the state file cannot be written (disk full, permissions), the error is logged but execution continues. A crash at this point means recovery is impossible. Consider treating state file write failures as fatal after a retry, or writing to a fallback location.

---

### 4. Observability

**Current state:** Logging to console and file. No metrics, no structured output.

**Improvements that would help diagnose looping failures:**

- **Structured log events.** Emit JSON log lines for each outcome so they can be piped to a log aggregator or `jq`-parsed.

- **Prometheus metrics.** A simple HTTP endpoint exposing `whittler_beads_processed_total{outcome="..."}`, `whittler_active_lanes`, and `whittler_last_poll_timestamp` would make looping failures immediately visible in a dashboard.

- **Webhook / notification on repeated failure.** After a bead fails `max_retries` times, post to a Slack webhook or GitHub issue rather than silently deferring.

---

### 5. Conflict recovery

**Current behaviour:** Conflict beads are preserved in state and left claimed. Nothing else happens.

**What's missing:**
- No notification to the operator that a conflict occurred
- No way to see all current conflicts without reading the state file
- The claimed bead blocks that work item indefinitely

**Improvements:**

- `whittler status` should highlight conflict beads prominently (different colour / section)
- Add a `whittler resolve <bead-id>` command that: opens the worktree in a shell, waits for the user to resolve and commit, then merges to main and closes the bead
- Or: after detecting a conflict, automatically open a GitHub PR from `bead/<id>` against `main` so the conflict can be resolved via code review tooling

---

### 6. No_changes handling

**Current behaviour:** If the agent exits 0 but made no file changes, the bead is unclaimed with outcome `no_changes`. It will be re-queued immediately.

**Risk:** A bead whose task is already done (file already exists) or whose design field points at the wrong path will loop as `no_changes` indefinitely.

**Improvement:** Same as indefinite-failure handling — track attempts and quarantine after N `no_changes` outcomes. Also log which files were inspected so the operator can see what the agent actually did.
