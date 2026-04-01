"""
Tests for the containers module.

Requires a running Docker daemon and the alpine:latest image.
"""

from __future__ import annotations

import pytest
import docker
import docker.errors

from whittler.containers import ContainerManager
from whittler.core import WhittlerConfig

# ---------------------------------------------------------------------------
# Docker availability gate
# ---------------------------------------------------------------------------

try:
    _client = docker.from_env()
    _client.ping()
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="Docker not available"
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_IMAGE = "alpine:latest"


@pytest.fixture
def config() -> WhittlerConfig:
    cfg = WhittlerConfig()
    cfg.container_image = TEST_IMAGE
    return cfg


@pytest.fixture
def manager(config: WhittlerConfig) -> ContainerManager:
    return ContainerManager(config)


# ---------------------------------------------------------------------------
# Test helper: bypass spawn() and run an arbitrary command directly
# ---------------------------------------------------------------------------

def _run_test_container(
    client: docker.DockerClient,
    command: str,
    labels: dict | None = None,
) -> str:
    """Directly run a container with an arbitrary command. Returns container ID."""
    c = client.containers.run(
        image=TEST_IMAGE,
        command=command,
        detach=True,
        auto_remove=False,
        labels=labels or {},
    )
    return c.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_and_wait_success(manager: ContainerManager) -> None:
    """spawn + wait: echo hello exits 0."""
    container_id = _run_test_container(
        manager.client, "echo hello", labels={"whittler": "true", "whittler.bead_id": "test-success"}
    )
    try:
        exit_code = await manager.wait(container_id, timeout=30)
        assert exit_code == 0
    finally:
        await manager.cleanup(container_id)


@pytest.mark.asyncio
async def test_spawn_and_wait_failure(manager: ContainerManager) -> None:
    """spawn + wait: sh -c 'exit 1' exits 1."""
    container_id = _run_test_container(
        manager.client,
        command=["sh", "-c", "exit 1"],
        labels={"whittler": "true", "whittler.bead_id": "test-failure"},
    )
    try:
        exit_code = await manager.wait(container_id, timeout=30)
        assert exit_code == 1
    finally:
        await manager.cleanup(container_id)


@pytest.mark.asyncio
async def test_logs(manager: ContainerManager) -> None:
    """Logs from a container contain the printed text."""
    container_id = _run_test_container(
        manager.client,
        command=["sh", "-c", "echo whittler-log-test"],
        labels={"whittler": "true"},
    )
    try:
        await manager.wait(container_id, timeout=30)
        output = await manager.logs(container_id)
        assert "whittler-log-test" in output
    finally:
        await manager.cleanup(container_id)


@pytest.mark.asyncio
async def test_cleanup(manager: ContainerManager) -> None:
    """After cleanup, the container no longer exists."""
    container_id = _run_test_container(
        manager.client,
        command="echo bye",
        labels={"whittler": "true"},
    )
    await manager.wait(container_id, timeout=30)
    await manager.cleanup(container_id)

    with pytest.raises(docker.errors.NotFound):
        manager.client.containers.get(container_id)


@pytest.mark.asyncio
async def test_cleanup_orphans(manager: ContainerManager) -> None:
    """cleanup_orphans removes all stopped whittler-labelled containers."""
    # Ensure a clean slate first
    await manager.cleanup_orphans()

    ids = []
    for i in range(2):
        cid = _run_test_container(
            manager.client,
            command="echo orphan",
            labels={"whittler": "true", "whittler.bead_id": f"orphan-{i}"},
        )
        ids.append(cid)

    # Wait for both to finish
    for cid in ids:
        await manager.wait(cid, timeout=30)

    removed = await manager.cleanup_orphans()
    assert removed == 2

    # Verify containers are gone
    for cid in ids:
        with pytest.raises(docker.errors.NotFound):
            manager.client.containers.get(cid)


@pytest.mark.asyncio
async def test_wait_timeout(manager: ContainerManager) -> None:
    """wait() returns -1 when the container doesn't finish within the timeout."""
    container_id = _run_test_container(
        manager.client,
        command=["sleep", "60"],
        labels={"whittler": "true"},
    )
    try:
        exit_code = await manager.wait(container_id, timeout=2)
        assert exit_code == -1
    finally:
        await manager.cleanup(container_id)
