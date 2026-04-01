"""
Tests for the orchestrator module.
"""

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

from whittler.core import BeadConfig, BeadRecord, BeadState, WhittlerConfig
from whittler.orchestrator import Orchestrator


def make_config(**kwargs) -> WhittlerConfig:
    defaults = dict(
        repo_root="/fake/repo",
        max_lanes=2,
        poll_interval=5,
        agent_timeout=900,
        max_retries=3,
        container_image="test-image:latest",
        worktree_base=".worktrees",
        state_file="/tmp/test-state.json",
        lock_file="/tmp/test.lock",
    )
    defaults.update(kwargs)
    return WhittlerConfig(**defaults)


def make_bead(bead_id: str = "bead-1") -> BeadConfig:
    return BeadConfig(
        id=bead_id,
        description=f"Test bead {bead_id}",
        design="",
        notes="",
    )


def make_orchestrator(config: WhittlerConfig | None = None) -> Orchestrator:
    """Create an Orchestrator with a mocked ContainerManager."""
    if config is None:
        config = make_config()
    with patch("whittler.orchestrator.ContainerManager") as MockCM:
        mock_cm = MagicMock()
        MockCM.return_value = mock_cm
        orch = Orchestrator(config)
    orch._container_mgr = mock_cm
    return orch


# ---------------------------------------------------------------------------
# Test 1: process_bead success → outcome == "merged"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_bead_success():
    orch = make_orchestrator()
    bead = make_bead()

    orch._container_mgr.spawn = AsyncMock(return_value="ctr-1")
    orch._container_mgr.wait = AsyncMock(return_value=0)
    orch._container_mgr.cleanup = AsyncMock()
    orch._container_mgr.logs = AsyncMock(return_value="")

    with (
        patch("whittler.orchestrator.beads.claim", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.close", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.unclaim", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.feedback", AsyncMock(return_value=True)),
        patch(
            "whittler.orchestrator.git.create_worktree",
            AsyncMock(return_value=("/wt/bead-1", "bead/bead-1")),
        ),
        patch("whittler.orchestrator.git.commit_worktree", AsyncMock(return_value=True)),
        patch(
            "whittler.orchestrator.git.merge_to_main",
            AsyncMock(return_value=(True, "file1.py")),
        ),
        patch("whittler.orchestrator.git.remove_worktree", AsyncMock()),
        patch.object(orch, "_save_state"),
    ):
        record = await orch.process_bead(bead)

    assert record.outcome == "merged"
    assert record.state == BeadState.Closed


# ---------------------------------------------------------------------------
# Test 2: process_bead claim fails → outcome == "claim_failed"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_bead_claim_fails():
    orch = make_orchestrator()
    bead = make_bead()

    with (
        patch("whittler.orchestrator.beads.claim", AsyncMock(return_value=False)),
        patch.object(orch, "_save_state"),
    ):
        record = await orch.process_bead(bead)

    assert record.outcome == "claim_failed"
    assert record.state == BeadState.Failed


# ---------------------------------------------------------------------------
# Test 3: container exits 1 → outcome == "agent_failed"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_bead_agent_failed():
    orch = make_orchestrator()
    bead = make_bead()

    orch._container_mgr.spawn = AsyncMock(return_value="ctr-1")
    orch._container_mgr.wait = AsyncMock(return_value=1)
    orch._container_mgr.cleanup = AsyncMock()
    orch._container_mgr.logs = AsyncMock(return_value="agent error output")

    with (
        patch("whittler.orchestrator.beads.claim", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.unclaim", AsyncMock(return_value=True)),
        patch(
            "whittler.orchestrator.git.create_worktree",
            AsyncMock(return_value=("/wt/bead-1", "bead/bead-1")),
        ),
        patch("whittler.orchestrator.git.remove_worktree", AsyncMock()),
        patch.object(orch, "_save_state"),
    ):
        record = await orch.process_bead(bead)

    assert record.outcome == "agent_failed"
    assert record.state == BeadState.Failed
    assert any("Container exited 1" in e for e in record.errors)


# ---------------------------------------------------------------------------
# Test 4: container exits -1 (timeout) → outcome == "timeout"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_bead_timeout():
    orch = make_orchestrator()
    bead = make_bead()

    orch._container_mgr.spawn = AsyncMock(return_value="ctr-1")
    orch._container_mgr.wait = AsyncMock(return_value=-1)
    orch._container_mgr.cleanup = AsyncMock()
    orch._container_mgr.logs = AsyncMock(return_value="partial logs")

    with (
        patch("whittler.orchestrator.beads.claim", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.unclaim", AsyncMock(return_value=True)),
        patch(
            "whittler.orchestrator.git.create_worktree",
            AsyncMock(return_value=("/wt/bead-1", "bead/bead-1")),
        ),
        patch("whittler.orchestrator.git.remove_worktree", AsyncMock()),
        patch.object(orch, "_save_state"),
    ):
        record = await orch.process_bead(bead)

    assert record.outcome == "timeout"
    assert record.state == BeadState.Failed
    assert any("timed out" in e for e in record.errors)


# ---------------------------------------------------------------------------
# Test 5: merge returns (False, ...) → outcome == "conflict"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_bead_merge_conflict():
    orch = make_orchestrator()
    bead = make_bead()

    orch._container_mgr.spawn = AsyncMock(return_value="ctr-1")
    orch._container_mgr.wait = AsyncMock(return_value=0)
    orch._container_mgr.cleanup = AsyncMock()
    orch._container_mgr.logs = AsyncMock(return_value="")

    with (
        patch("whittler.orchestrator.beads.claim", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.close", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.unclaim", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.feedback", AsyncMock(return_value=True)),
        patch(
            "whittler.orchestrator.git.create_worktree",
            AsyncMock(return_value=("/wt/bead-1", "bead/bead-1")),
        ),
        patch("whittler.orchestrator.git.commit_worktree", AsyncMock(return_value=True)),
        patch(
            "whittler.orchestrator.git.merge_to_main",
            AsyncMock(return_value=(False, "conflicted_file.py")),
        ),
        patch("whittler.orchestrator.git.remove_worktree", AsyncMock()),
        patch.object(orch, "_save_state"),
    ):
        record = await orch.process_bead(bead)

    assert record.outcome == "conflict"
    assert record.state == BeadState.Failed


# ---------------------------------------------------------------------------
# Test 6: run loop processes a batch of 2 beads
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_loop_processes_batch():
    config = make_config(poll_interval=0)
    orch = make_orchestrator(config)

    bead1 = make_bead("b1")
    bead2 = make_bead("b2")

    call_count = 0

    async def fake_ready(repo_root):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [bead1, bead2]
        # After first batch, trigger shutdown and return empty
        orch._shutdown.set()
        return []

    process_calls = []

    async def fake_process_bead(bead):
        process_calls.append(bead.id)
        rec = BeadRecord(
            config=bead,
            state=BeadState.Closed,
            branch="",
            worktree_path="",
            container_id="",
            outcome="merged",
        )
        return rec

    with (
        patch("whittler.orchestrator.beads.ready", side_effect=fake_ready),
        patch("whittler.orchestrator.git.verify_repo_health", AsyncMock()),
        patch("whittler.orchestrator.git.cleanup_stale_worktrees", AsyncMock(return_value=[])),
        patch.object(orch._container_mgr, "cleanup_orphans", AsyncMock(return_value=0)),
        patch.object(orch, "_load_state", return_value={}),
        patch.object(orch, "_save_state"),
        patch.object(orch, "_process_bead_inner", side_effect=fake_process_bead),
        patch("fcntl.flock"),
        patch("builtins.open", MagicMock()),
    ):
        await orch.run()

    assert set(process_calls) == {"b1", "b2"}


# ---------------------------------------------------------------------------
# Test 7: concurrency respects max_lanes=1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrency_respects_max_lanes():
    config = make_config(max_lanes=1)
    orch = make_orchestrator(config)

    concurrent_count = 0
    max_concurrent = 0
    order = []

    async def slow_process(bead):
        nonlocal concurrent_count, max_concurrent
        concurrent_count += 1
        max_concurrent = max(max_concurrent, concurrent_count)
        order.append(f"start-{bead.id}")
        await asyncio.sleep(0.01)
        order.append(f"end-{bead.id}")
        concurrent_count -= 1
        return BeadRecord(
            config=bead,
            state=BeadState.Closed,
            branch="",
            worktree_path="",
            container_id="",
            outcome="merged",
        )

    beads_list = [make_bead(f"bead-{i}") for i in range(3)]

    with patch.object(orch, "_process_bead_inner", side_effect=slow_process):
        tasks = [asyncio.create_task(orch.process_bead(b)) for b in beads_list]
        await asyncio.gather(*tasks)

    assert max_concurrent == 1, f"Expected max 1 concurrent, got {max_concurrent}"


# ---------------------------------------------------------------------------
# Test 8: handle_signal sets shutdown event
# ---------------------------------------------------------------------------

def test_handle_signal():
    orch = make_orchestrator()
    assert not orch._shutdown.is_set()
    mock_loop = MagicMock()
    with patch("asyncio.get_running_loop", return_value=mock_loop):
        orch.handle_signal(signal.SIGTERM)
    assert orch._shutdown.is_set()


# ---------------------------------------------------------------------------
# Test 9: attempt counter increments and quarantines
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_attempts_increment_and_quarantine():
    """After max_retries agent_failed attempts, bead is quarantined, not unclaimed."""
    config = make_config(max_retries=2)
    orch = make_orchestrator(config)
    bead = make_bead()

    orch._container_mgr.spawn = AsyncMock(return_value="ctr-1")
    orch._container_mgr.wait = AsyncMock(return_value=1)  # agent_failed
    orch._container_mgr.cleanup = AsyncMock()
    orch._container_mgr.logs = AsyncMock(return_value="error")

    mock_unclaim = AsyncMock(return_value=True)
    mock_update_status = AsyncMock(return_value=True)

    with (
        patch("whittler.orchestrator.beads.claim", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.unclaim", mock_unclaim),
        patch("whittler.orchestrator.beads.update_status", mock_update_status),
        patch(
            "whittler.orchestrator.git.create_worktree",
            AsyncMock(return_value=("/wt/bead-1", "bead/bead-1")),
        ),
        patch("whittler.orchestrator.git.remove_worktree", AsyncMock()),
        patch.object(orch, "_save_state"),
    ):
        # First attempt
        record1 = await orch.process_bead(bead)
        assert record1.attempts == 1
        assert mock_unclaim.call_count == 1
        mock_update_status.assert_not_called()
        mock_unclaim.reset_mock()

        # Second attempt — hits max_retries
        record2 = await orch.process_bead(bead)
        assert record2.attempts == 2
        mock_update_status.assert_called_once_with(bead.id, "deferred", orch.config.repo_root)
        mock_unclaim.assert_not_called()


# ---------------------------------------------------------------------------
# Test 10: _save_state retries on first OSError and raises on second
# ---------------------------------------------------------------------------

def test_save_state_retries_and_raises():
    orch = make_orchestrator()
    orch._state = {}

    call_count = 0
    def failing_open(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise OSError("disk full")

    with patch("builtins.open", side_effect=failing_open):
        with pytest.raises(OSError):
            orch._save_state()

    assert call_count == 2


# ---------------------------------------------------------------------------
# Test 11: handle_signal schedules force shutdown
# ---------------------------------------------------------------------------

def test_handle_signal_schedules_force_shutdown():
    orch = make_orchestrator()
    mock_loop = MagicMock()
    with patch("asyncio.get_running_loop", return_value=mock_loop):
        orch.handle_signal(signal.SIGTERM)
    assert orch._shutdown.is_set()
    mock_loop.call_later.assert_called_once_with(
        orch.config.shutdown_timeout, orch._force_shutdown
    )


# ---------------------------------------------------------------------------
# Test 12: CancelledError triggers unclaim
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancelled_error_unclaims_bead():
    orch = make_orchestrator()
    bead = make_bead()
    mock_unclaim = AsyncMock(return_value=True)

    async def raise_cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    with (
        patch("whittler.orchestrator.beads.claim", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.unclaim", mock_unclaim),
        patch(
            "whittler.orchestrator.git.create_worktree",
            side_effect=raise_cancelled,
        ),
        patch.object(orch, "_save_state"),
    ):
        with pytest.raises(asyncio.CancelledError):
            await orch._process_bead_inner(bead)

    mock_unclaim.assert_called_once_with(bead.id, orch.config.repo_root)


# ---------------------------------------------------------------------------
# Test 13: generic Exception triggers best-effort unclaim
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exception_in_worktree_creation_unclames_bead():
    """An unexpected exception after claim triggers best-effort unclaim."""
    orch = make_orchestrator()
    bead = make_bead()
    mock_unclaim = AsyncMock(return_value=True)

    with (
        patch("whittler.orchestrator.beads.claim", AsyncMock(return_value=True)),
        patch("whittler.orchestrator.beads.unclaim", mock_unclaim),
        patch(
            "whittler.orchestrator.git.create_worktree",
            AsyncMock(side_effect=RuntimeError("disk error")),
        ),
        patch.object(orch, "_save_state"),
    ):
        record = await orch.process_bead(bead)

    assert record.outcome == "error"
    assert record.state == BeadState.Failed
    mock_unclaim.assert_called_once_with(bead.id, orch.config.repo_root)
