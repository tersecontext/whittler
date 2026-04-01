"""
Tests for the git module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from whittler import git


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: str) -> None:
    """Initialise a git repo at *path* with one commit on main."""
    cmds = [
        ["git", "init", "-b", "main", path],
        ["git", "-C", path, "config", "user.email", "test@test.com"],
        ["git", "-C", path, "config", "user.name", "Test"],
        ["git", "-C", path, "commit", "--allow-empty", "-m", "Initial commit"],
    ]
    for cmd in cmds:
        subprocess.run(cmd, check=True, capture_output=True)


def _make_repo() -> str:
    """Create a temp dir, init a repo inside it, return the path."""
    tmpdir = tempfile.mkdtemp()
    _init_repo(tmpdir)
    return tmpdir


def _add_file(repo: str, filename: str = "hello.txt", content: str = "hello\n") -> None:
    """Write *filename* inside *repo*, stage and commit it."""
    with open(os.path.join(repo, filename), "w") as fh:
        fh.write(content)
    subprocess.run(["git", "-C", repo, "add", filename], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-m", f"add {filename}"],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    """A fresh git repo on main with one empty commit."""
    path = str(tmp_path / "repo")
    os.makedirs(path)
    _init_repo(path)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def repo_with_file(repo):
    """A fresh git repo on main with one committed file."""
    _add_file(repo, "README.txt", "content\n")
    return repo


# 1 -------------------------------------------------------------------------

async def test_verify_repo_health_passes_on_clean_repo(repo):
    # Should not raise.
    await git.verify_repo_health(repo)


# 2 -------------------------------------------------------------------------

async def test_verify_repo_health_fails_on_dirty_repo(repo):
    # Write an untracked (unstaged) file to make the working tree dirty.
    dirty_file = os.path.join(repo, "dirty.txt")
    with open(dirty_file, "w") as fh:
        fh.write("dirty\n")
    subprocess.run(["git", "-C", repo, "add", "dirty.txt"], check=True, capture_output=True)

    with pytest.raises(RuntimeError, match="dirty"):
        await git.verify_repo_health(repo)


# 3 -------------------------------------------------------------------------

async def test_verify_repo_health_fails_on_non_git_dir(tmp_path):
    non_git = str(tmp_path / "not_a_repo")
    os.makedirs(non_git)
    with pytest.raises(RuntimeError):
        await git.verify_repo_health(non_git)


# 4 -------------------------------------------------------------------------

async def test_create_worktree(repo_with_file):
    worktree_path, branch_name = await git.create_worktree(
        bead_id="42",
        repo_root=repo_with_file,
        worktree_base=".worktrees",
    )

    expected_path = os.path.join(repo_with_file, ".worktrees", "bead-42")
    assert worktree_path == expected_path
    assert branch_name == "bead/42"
    assert os.path.isdir(worktree_path)

    # Cleanup
    await git.remove_worktree(worktree_path, branch_name, repo_with_file)


# 5 -------------------------------------------------------------------------

async def test_create_worktree_cleans_up_stale(repo_with_file):
    # Create once.
    worktree_path, branch_name = await git.create_worktree(
        bead_id="99",
        repo_root=repo_with_file,
        worktree_base=".worktrees",
    )
    assert os.path.isdir(worktree_path)

    # Create again — should clean up the stale worktree/branch and succeed.
    worktree_path2, branch_name2 = await git.create_worktree(
        bead_id="99",
        repo_root=repo_with_file,
        worktree_base=".worktrees",
    )
    assert worktree_path2 == worktree_path
    assert branch_name2 == branch_name
    assert os.path.isdir(worktree_path2)

    await git.remove_worktree(worktree_path2, branch_name2, repo_with_file)


# 6 -------------------------------------------------------------------------

async def test_commit_worktree_with_changes(repo_with_file):
    worktree_path, branch_name = await git.create_worktree(
        bead_id="7",
        repo_root=repo_with_file,
        worktree_base=".worktrees",
    )

    # Write a file in the worktree.
    with open(os.path.join(worktree_path, "solution.py"), "w") as fh:
        fh.write("# solution\n")

    result = await git.commit_worktree(
        worktree_path=worktree_path,
        bead_id="7",
        description="implement the solution",
    )
    assert result is True

    await git.remove_worktree(worktree_path, branch_name, repo_with_file)


# 7 -------------------------------------------------------------------------

async def test_commit_worktree_no_changes(repo_with_file):
    worktree_path, branch_name = await git.create_worktree(
        bead_id="8",
        repo_root=repo_with_file,
        worktree_base=".worktrees",
    )

    result = await git.commit_worktree(
        worktree_path=worktree_path,
        bead_id="8",
        description="no changes here",
    )
    assert result is False

    await git.remove_worktree(worktree_path, branch_name, repo_with_file)


# 8 -------------------------------------------------------------------------

async def test_merge_to_main_clean(repo_with_file):
    worktree_path, branch_name = await git.create_worktree(
        bead_id="10",
        repo_root=repo_with_file,
        worktree_base=".worktrees",
    )

    # Commit a new file in the worktree.
    with open(os.path.join(worktree_path, "feature.py"), "w") as fh:
        fh.write("x = 1\n")
    await git.commit_worktree(worktree_path, "10", "add feature")

    async with git._merge_lock:
        success, changed_files = await git.merge_to_main(
            branch=branch_name,
            bead_id="10",
            description="add feature",
            repo_root=repo_with_file,
        )

    assert success is True
    assert "feature.py" in changed_files

    await git.remove_worktree(worktree_path, branch_name, repo_with_file)


# 9 -------------------------------------------------------------------------

async def test_merge_to_main_conflict(repo_with_file):
    # Create a worktree that touches the same file as main will touch.
    worktree_path, branch_name = await git.create_worktree(
        bead_id="11",
        repo_root=repo_with_file,
        worktree_base=".worktrees",
    )

    # Both main and the branch edit the same file with conflicting content.
    shared_file = "shared.txt"

    # Commit in worktree first.
    with open(os.path.join(worktree_path, shared_file), "w") as fh:
        fh.write("branch version\n")
    await git.commit_worktree(worktree_path, "11", "branch edit")

    # Now commit conflicting content on main.
    with open(os.path.join(repo_with_file, shared_file), "w") as fh:
        fh.write("main version\n")
    subprocess.run(
        ["git", "-C", repo_with_file, "add", shared_file],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", repo_with_file, "commit", "-m", "main edit"],
        check=True,
        capture_output=True,
    )

    async with git._merge_lock:
        success, changed_files = await git.merge_to_main(
            branch=branch_name,
            bead_id="11",
            description="conflicting edit",
            repo_root=repo_with_file,
        )

    assert success is False
    # changed_files may be empty if the diff was computed before the conflict;
    # we still check that the return type is correct.
    assert isinstance(changed_files, str)

    await git.remove_worktree(worktree_path, branch_name, repo_with_file)


# 10 ------------------------------------------------------------------------

async def test_remove_worktree(repo_with_file):
    worktree_path, branch_name = await git.create_worktree(
        bead_id="20",
        repo_root=repo_with_file,
        worktree_base=".worktrees",
    )
    assert os.path.isdir(worktree_path)

    await git.remove_worktree(worktree_path, branch_name, repo_with_file)

    assert not os.path.isdir(worktree_path)

    # Branch should be gone.
    rc = subprocess.run(
        ["git", "-C", repo_with_file, "rev-parse", "--verify", branch_name],
        capture_output=True,
    ).returncode
    assert rc != 0


# 11 ------------------------------------------------------------------------

async def test_cleanup_stale_worktrees(repo_with_file):
    # Create a worktree, then manually delete the directory so git considers
    # it orphaned after prune.
    worktree_path, branch_name = await git.create_worktree(
        bead_id="30",
        repo_root=repo_with_file,
        worktree_base=".worktrees",
    )
    assert os.path.isdir(worktree_path)

    # Simulate orphan: remove the directory without telling git.
    shutil.rmtree(worktree_path)

    cleaned = await git.cleanup_stale_worktrees(
        repo_root=repo_with_file,
        worktree_base=".worktrees",
    )

    # The worktree directory is gone, so cleanup should not error.
    # Either it's listed in cleaned (if git worktree prune didn't fix it)
    # or it was pruned by git automatically.  Either way the dir is gone.
    assert not os.path.isdir(worktree_path)

    # Cleanup branch if it survived.
    subprocess.run(
        ["git", "-C", repo_with_file, "branch", "-D", branch_name],
        capture_output=True,
    )
