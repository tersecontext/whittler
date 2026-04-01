import asyncio
import json
import os
import subprocess
import tempfile
import pytest
import docker

# Skip if Docker not available
try:
    docker.from_env().ping()
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available")

INTEGRATION_DIR = os.path.dirname(__file__)
TEST_IMAGE = "whittler-test-solver:latest"


@pytest.fixture(scope="module")
def test_image():
    """Build the test solver image once per test session."""
    client = docker.from_env()
    client.images.build(
        path=INTEGRATION_DIR,
        dockerfile="Dockerfile.test",
        tag=TEST_IMAGE,
        rm=True,
    )
    yield TEST_IMAGE
    # Cleanup: remove the image after tests
    try:
        client.images.remove(TEST_IMAGE, force=True)
    except Exception:
        pass


@pytest.fixture
def temp_repo():
    """Create a temp git repo with an initial commit on branch 'main'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(
            ["git", "init", "-b", "main", tmpdir], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", tmpdir, "config", "user.email", "test@test.com"], check=True
        )
        subprocess.run(
            ["git", "-C", tmpdir, "config", "user.name", "Test"], check=True
        )
        # Create initial commit
        readme = os.path.join(tmpdir, "README.md")
        with open(readme, "w") as f:
            f.write("# Test repo\n")
        subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", tmpdir, "commit", "-m", "Initial commit"],
            check=True,
            capture_output=True,
        )
        yield tmpdir


def test_full_loop(test_image, temp_repo):
    """
    Full e2e test:
    1. Set PATH to include mock_bd.sh directory
    2. Create WhittlerConfig pointing to temp_repo and test_image
    3. Run orchestrator for one cycle (shutdown after first batch)
    4. Assert: hello.txt exists in the repo on main
    5. Assert: commit message contains bead ID
    """
    import shutil
    from whittler.core import WhittlerConfig
    from whittler.orchestrator import Orchestrator
    from whittler import beads, git

    original_path = os.environ.get("PATH", "")

    # Use TemporaryDirectory context manager to eliminate resource leak
    with tempfile.TemporaryDirectory() as mock_bd_dir:
        # Make mock_bd.sh available as "bd" in PATH
        mock_bd_path = os.path.join(mock_bd_dir, "bd")
        os.symlink(os.path.join(INTEGRATION_DIR, "mock_bd.sh"), mock_bd_path)
        os.chmod(mock_bd_path, 0o755)

        # Use unique temp file for mock_bd claimed state (parallel-safe)
        state_file = os.path.join(mock_bd_dir, "bd_state")

        config = WhittlerConfig(
            repo_root=temp_repo,
            container_image=TEST_IMAGE,
            max_lanes=1,
            poll_interval=1,
            agent_timeout=30,
            worktree_base=".worktrees",
            state_file=os.path.join(temp_repo, ".whittler-state.json"),
            lock_file=os.path.join(temp_repo, ".whittler.lock"),
        )

        os.environ["PATH"] = f"{mock_bd_dir}:{original_path}"
        os.environ["MOCK_BD_STATE_FILE"] = state_file

        try:
            async def run_one_cycle():
                orch = Orchestrator(config)

                # Perform startup cleanup (Orchestrator._startup_cleanup does not exist;
                # call the constituent methods directly instead).
                try:
                    await git.verify_repo_health(config.repo_root)
                except Exception:
                    pass
                try:
                    await git.cleanup_stale_worktrees(config.repo_root, config.worktree_base)
                except Exception:
                    pass
                try:
                    await orch._container_mgr.cleanup_orphans()
                except Exception:
                    pass

                ready_beads = await beads.ready(config.repo_root)
                if ready_beads:
                    tasks = [
                        asyncio.create_task(orch.process_bead(bead))
                        for bead in ready_beads
                    ]
                    records = await asyncio.gather(*tasks, return_exceptions=True)
                    return records
                return []

            records = asyncio.run(run_one_cycle())

            # Assert the bead was processed successfully
            assert len(records) == 1
            record = records[0]
            assert not isinstance(record, Exception), f"process_bead raised: {record}"
            assert record.outcome == "merged", (
                f"Expected merged, got {record.outcome}: {record.errors}"
            )

            # Assert hello.txt exists on main branch
            hello_path = os.path.join(temp_repo, "hello.txt")
            assert os.path.exists(hello_path), "hello.txt should exist after merge"
            with open(hello_path) as f:
                content = f.read().strip()
            assert "test-bead-001" in content, (
                f"hello.txt should mention the bead id, got: {content}"
            )

            # Assert commit message contains bead ID
            result = subprocess.run(
                ["git", "-C", temp_repo, "log", "--oneline", "-3"],
                capture_output=True,
                text=True,
            )
            assert "test-bead-001" in result.stdout, (
                f"Commit should mention bead id. Log: {result.stdout}"
            )

        finally:
            os.environ["PATH"] = original_path
            if "MOCK_BD_STATE_FILE" in os.environ:
                del os.environ["MOCK_BD_STATE_FILE"]
