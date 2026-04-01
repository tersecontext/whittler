"""
Main orchestrator for Whittler.

This module coordinates polling for beads, creating worktrees, spawning
containers, and managing the complete workflow for processing work units.
"""

import asyncio
import fcntl
import json
import logging
import os
import signal
import time

from whittler.core import BeadConfig, BeadRecord, BeadState, WhittlerConfig
from whittler import beads, git
from whittler.containers import ContainerManager

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: WhittlerConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self._semaphore = asyncio.Semaphore(config.max_lanes)
        self._merge_lock = asyncio.Lock()  # serializes merges; one merge at a time
        self._container_mgr = ContainerManager(config)
        self._state: dict[str, BeadRecord] = {}  # bead_id -> BeadRecord
        self._shutdown = asyncio.Event()
        self._lock_fd = None  # file descriptor for process lock
        self._active_tasks: set[asyncio.Task] = set()
        self._attempt_counts: dict[str, int] = {}

    async def run(self) -> None:
        """Main loop. Polls for beads and processes them in batches."""
        # 1. Acquire process lock (fcntl.flock) on config.lock_file
        lock_path = self.config.lock_file
        self._lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_fd.close()
            self._lock_fd = None
            raise RuntimeError(f"Another Whittler instance is running (lock: {lock_path})")

        try:
            # 2. Load state from config.state_file (crash recovery)
            self._state = self._load_state()

            # 3. Verify repo health
            await git.verify_repo_health(self.config.repo_root)

            # 4. Cleanup stale worktrees and orphan containers
            await git.cleanup_stale_worktrees(self.config.repo_root, self.config.worktree_base)
            await self._container_mgr.cleanup_orphans()

            # 5. Loop until _shutdown
            while not self._shutdown.is_set():
                # a. Poll beads.ready
                ready_beads = await beads.ready(self.config.repo_root)

                # b. If no beads: sleep config.poll_interval, continue
                if not ready_beads:
                    try:
                        await asyncio.wait_for(
                            self._shutdown.wait(),
                            timeout=self.config.poll_interval,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue

                # c. Dispatch tasks bounded by semaphore
                tasks = [asyncio.create_task(self.process_bead(bead)) for bead in ready_beads]

                for task in tasks:
                    self._active_tasks.add(task)
                    task.add_done_callback(self._active_tasks.discard)

                # d. await gather
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # e. Log results
                for bead, result in zip(ready_beads, results):
                    if isinstance(result, Exception):
                        self.logger.error(
                            "Task for bead %s raised exception: %s", bead.id, result
                        )
                    else:
                        self.logger.info(
                            "Bead %s completed with outcome: %s",
                            bead.id,
                            result.outcome if isinstance(result, BeadRecord) else "unknown",
                        )

                # f. Brief pause before next poll
                if not self._shutdown.is_set():
                    try:
                        await asyncio.wait_for(self._shutdown.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass

        finally:
            # 6. Release lock on shutdown
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                self._lock_fd.close()
                self._lock_fd = None

    async def _process_bead_inner(self, bead: BeadConfig) -> BeadRecord:
        """Inner implementation of process_bead without semaphore acquisition."""
        attempts_so_far = self._attempt_counts.get(bead.id, 0)
        record = BeadRecord(
            config=bead,
            state=BeadState.Ready,
            branch="",
            worktree_path="",
            container_id="",
            attempts=attempts_so_far,
        )

        # 1. Claim bead
        claimed = await beads.claim(bead.id, self.config.repo_root)
        if not claimed:
            record.state = BeadState.Failed
            record.outcome = "claim_failed"
            return record

        record.state = BeadState.Claimed
        record.claimed_at = time.time()
        self._log_event("claimed", bead.id)

        try:
            # 2. Create worktree
            worktree_path, branch = await git.create_worktree(
                bead.id, self.config.repo_root, self.config.worktree_base
            )
            record.branch = branch
            record.worktree_path = worktree_path

            # 3. Save state (crash recovery)
            record.state = BeadState.Solving
            self._state[bead.id] = record
            self._save_state()
            self._log_event("solving", bead.id, branch=branch)

            # 4. Spawn container
            container_id = await self._container_mgr.spawn(bead, worktree_path)
            record.container_id = container_id

            # 5. Wait for container
            exit_code = await self._container_mgr.wait(container_id, self.config.agent_timeout)

            if exit_code == 0:
                # 6a. Success path
                committed = await git.commit_worktree(worktree_path, bead.id, bead.description)
                if committed:
                    record.state = BeadState.Merging
                    self._save_state()

                    async with self._merge_lock:
                        merged, changed_files = await git.merge_to_main(
                            branch, bead.id, bead.description, self.config.repo_root
                        )

                    if merged:
                        record.state = BeadState.Closed
                        record.outcome = "merged"
                        record.completed_at = time.time()
                        await git.remove_worktree(worktree_path, branch, self.config.repo_root)
                        await beads.close(bead.id, self.config.repo_root)
                        await beads.feedback(bead.id, bead.description, changed_files, self.config.repo_root)
                        self._log_event("merged", bead.id, branch=branch)
                    else:
                        # Merge conflict — preserve worktree for human review
                        # Intentionally leave bead claimed — conflict requires human resolution.
                        # A claimed bead won't be returned by beads.ready(), preventing duplicate work.
                        # Human must manually unclaim or resolve after fixing the conflict.
                        record.state = BeadState.Failed
                        record.outcome = "conflict"
                        self.logger.error(
                            "Merge conflict for bead %s, branch %s preserved for review",
                            bead.id, branch
                        )
                        self._log_event("conflict", bead.id, branch=branch)
                else:
                    # Nothing to commit (agent made no changes)
                    record.state = BeadState.Failed
                    record.outcome = "no_changes"
                    record.attempts += 1
                    self._attempt_counts[bead.id] = record.attempts
                    if record.attempts >= self.config.max_retries:
                        await beads.update_status(bead.id, "deferred", self.config.repo_root)
                        self.logger.warning(
                            "Bead %s quarantined after %d failures (outcome: %s)",
                            bead.id, record.attempts, record.outcome,
                        )
                        self._log_event("quarantined", bead.id, attempts=record.attempts)
                    else:
                        await beads.unclaim(bead.id, self.config.repo_root)
                    self._log_event("failed", bead.id, outcome=record.outcome, attempts=record.attempts)
                    await git.remove_worktree(worktree_path, branch, self.config.repo_root)

            elif exit_code == -1:
                # Timeout
                logs = await self._container_mgr.logs(container_id)
                self.logger.error("Bead %s timed out. Last logs:\n%s", bead.id, logs[-2000:])
                record.state = BeadState.Failed
                record.outcome = "timeout"
                record.errors.append(f"Container timed out after {self.config.agent_timeout}s")
                record.attempts += 1
                self._attempt_counts[bead.id] = record.attempts
                if record.attempts >= self.config.max_retries:
                    await beads.update_status(bead.id, "deferred", self.config.repo_root)
                    self.logger.warning(
                        "Bead %s quarantined after %d failures (outcome: %s)",
                        bead.id, record.attempts, record.outcome,
                    )
                    self._log_event("quarantined", bead.id, attempts=record.attempts)
                else:
                    await beads.unclaim(bead.id, self.config.repo_root)
                self._log_event("failed", bead.id, outcome=record.outcome, attempts=record.attempts)
                await git.remove_worktree(worktree_path, branch, self.config.repo_root)

            else:
                # Agent failure (exit_code == 1 or other non-zero)
                logs = await self._container_mgr.logs(container_id)
                self.logger.error("Bead %s failed (exit %d). Logs:\n%s", bead.id, exit_code, logs[-2000:])
                record.state = BeadState.Failed
                record.outcome = "agent_failed"
                record.errors.append(f"Container exited {exit_code}")
                record.attempts += 1
                self._attempt_counts[bead.id] = record.attempts
                if record.attempts >= self.config.max_retries:
                    await beads.update_status(bead.id, "deferred", self.config.repo_root)
                    self.logger.warning(
                        "Bead %s quarantined after %d failures (outcome: %s)",
                        bead.id, record.attempts, record.outcome,
                    )
                    self._log_event("quarantined", bead.id, attempts=record.attempts)
                else:
                    await beads.unclaim(bead.id, self.config.repo_root)
                self._log_event("failed", bead.id, outcome=record.outcome, attempts=record.attempts)
                await git.remove_worktree(worktree_path, branch, self.config.repo_root)

        except asyncio.CancelledError:
            self.logger.warning("Bead %s cancelled during shutdown", bead.id)
            try:
                await beads.unclaim(bead.id, self.config.repo_root)
            except Exception:
                pass
            raise

        except Exception as e:
            self.logger.exception("Unexpected error processing bead %s", bead.id)
            record.state = BeadState.Failed
            record.outcome = "error"
            record.errors.append(str(e))
            self._log_event("error", bead.id, error=str(e))
            # Best-effort cleanup
            if record.container_id:
                await self._container_mgr.kill(record.container_id)
            if record.worktree_path:
                await git.remove_worktree(record.worktree_path, record.branch, self.config.repo_root)

        finally:
            if record.container_id:
                await self._container_mgr.cleanup(record.container_id)
            # Keep conflict-state beads in state so humans can see them on restart
            if record.outcome != "conflict":
                self._state.pop(bead.id, None)
            self._save_state()

        return record

    async def process_bead(self, bead: BeadConfig) -> BeadRecord:
        """Full lifecycle for one bead."""
        async with self._semaphore:
            return await self._process_bead_inner(bead)

    def handle_signal(self, sig):
        """Set shutdown event. Current batch finishes, no new beads claimed."""
        self.logger.info("Received signal %s, initiating graceful shutdown", sig)
        self._shutdown.set()
        loop = asyncio.get_running_loop()
        loop.call_later(self.config.shutdown_timeout, self._force_shutdown)

    def _force_shutdown(self):
        self.logger.warning("Drain timeout reached; cancelling %d active tasks", len(self._active_tasks))
        for task in list(self._active_tasks):
            task.cancel()

    def _log_event(self, event: str, bead_id: str, **kwargs):
        """Emit a structured JSON log line for key lifecycle transitions."""
        import json as _json
        payload = {"event": event, "bead_id": bead_id, **kwargs}
        self.logger.info("WHITTLER_EVENT %s", _json.dumps(payload))

    def _save_state(self):
        """Persist in-flight bead records to state file."""
        state_data = {k: v.to_dict() for k, v in self._state.items()}
        for attempt in range(2):
            try:
                with open(self.config.state_file, "w") as f:
                    json.dump(state_data, f, indent=2)
                return
            except OSError as e:
                if attempt == 0:
                    self.logger.warning("State write failed (attempt 1), retrying: %s", e)
                else:
                    self.logger.error("State write failed twice; aborting: %s", e)
                    raise

    def _load_state(self) -> dict[str, BeadRecord]:
        """Load state from file for crash recovery."""
        try:
            with open(self.config.state_file) as f:
                data = json.load(f)
            return {k: BeadRecord.from_dict(v) for k, v in data.items()}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, KeyError) as e:
            self.logger.warning("Could not load state file: %s", e)
            return {}
