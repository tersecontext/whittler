"""
Git operations for Whittler.

This module handles git worktree creation, branch management, and commit
operations required for isolated bead processing.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# Merge lock — callers must acquire this before calling merge_to_main.
# The orchestrator imports and holds this lock during merge operations to
# prevent concurrent merges from racing on the main branch.
_merge_lock = asyncio.Lock()


async def _run_git(
    *args: str,
    cwd: str,
    timeout: float = 30.0,
    check: bool = True,
) -> tuple[int, str, str]:
    """Run a git command, returning (returncode, stdout, stderr).

    Args:
        *args: Arguments passed to git (not including "git" itself).
        cwd: Working directory for the subprocess.
        timeout: Seconds before asyncio.TimeoutError is raised.
        check: If True and returncode != 0, raise RuntimeError.
    """
    proc = await asyncio.wait_for(
        asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ),
        timeout=timeout,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        proc.communicate(), timeout=timeout
    )
    stdout = stdout_bytes.decode().strip()
    stderr = stderr_bytes.decode().strip()
    returncode = proc.returncode

    if check and returncode != 0:
        cmd = "git " + " ".join(args)
        raise RuntimeError(
            f"git command failed (rc={returncode}): {cmd!r}\nstderr: {stderr}"
        )

    return returncode, stdout, stderr


async def verify_repo_health(repo_root: str) -> None:
    """Verify that the repository at *repo_root* is in a state safe for Whittler.

    Checks:
    - Is it a valid git repository?
    - Is there an in-progress merge?
    - Is the current branch ``main``?
    - Is the main branch clean (no uncommitted changes)?

    Raises:
        RuntimeError: With a descriptive message on any failure.
    """
    # 1. Verify it is a git repository.
    rc, _, stderr = await _run_git(
        "rev-parse", "--git-dir",
        cwd=repo_root,
        timeout=10.0,
        check=False,
    )
    if rc != 0:
        raise RuntimeError(
            f"Not a git repository (or any parent): {repo_root!r}"
        )

    # 2. Check for in-progress merge.
    _, git_dir, _ = await _run_git(
        "rev-parse", "--git-dir", cwd=repo_root, timeout=10.0
    )
    # git_dir may be relative (e.g. ".git") or absolute.
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(repo_root, git_dir)
    merge_head = os.path.join(git_dir, "MERGE_HEAD")
    if os.path.exists(merge_head):
        raise RuntimeError(
            "Repository has an in-progress merge. Resolve it before running Whittler."
        )

    # 3. Verify current branch is main.
    _, branch, _ = await _run_git(
        "rev-parse", "--abbrev-ref", "HEAD",
        cwd=repo_root, timeout=10.0
    )
    if branch != "main":
        raise RuntimeError(
            f"Current branch is {branch!r}, expected 'main'. "
            "Whittler must be started from the main branch."
        )

    # 4. Verify main is clean (tracked files only; ignore untracked).
    _, status, _ = await _run_git(
        "status", "--porcelain", "-uno",
        cwd=repo_root, timeout=10.0
    )
    if status:
        raise RuntimeError(
            f"Working tree is dirty. Commit or stash changes before running Whittler.\n"
            f"Dirty files:\n{status}"
        )


async def create_worktree(
    bead_id: str,
    repo_root: str,
    worktree_base: str,
) -> tuple[str, str]:
    """Create a git worktree for the given bead.

    Branch name:    ``bead/<bead_id>``
    Worktree path:  ``<repo_root>/<worktree_base>/bead-<bead_id>``

    If a stale worktree or branch already exists they are forcibly removed
    before creating fresh ones.

    Args:
        bead_id: Unique identifier for the bead.
        repo_root: Root directory of the main repository checkout.
        worktree_base: Sub-directory (relative to *repo_root*) under which
            worktrees are created.

    Returns:
        ``(worktree_path, branch_name)``
    """
    branch_name = f"bead/{bead_id}"
    worktree_path = os.path.join(repo_root, worktree_base, f"bead-{bead_id}")

    # Clean up any stale worktree at the target path.
    if os.path.exists(worktree_path):
        logger.warning(
            "Stale worktree found at %s, removing it.", worktree_path
        )
        await _run_git(
            "worktree", "remove", "--force", worktree_path,
            cwd=repo_root, timeout=30.0, check=False
        )

    # Clean up any stale branch with the same name.
    rc, _, _ = await _run_git(
        "rev-parse", "--verify", branch_name,
        cwd=repo_root, timeout=10.0, check=False
    )
    if rc == 0:
        logger.warning("Stale branch %r found, deleting it.", branch_name)
        await _run_git(
            "branch", "-D", branch_name,
            cwd=repo_root, timeout=10.0, check=False
        )

    # Ensure the worktree base directory exists.
    worktree_dir = os.path.join(repo_root, worktree_base)
    os.makedirs(worktree_dir, exist_ok=True)

    # Create new worktree + branch from main.
    await _run_git(
        "worktree", "add", worktree_path, "-b", branch_name,
        cwd=repo_root, timeout=60.0
    )

    logger.info(
        "Created worktree %s on branch %s", worktree_path, branch_name
    )
    return worktree_path, branch_name


async def commit_worktree(
    worktree_path: str,
    bead_id: str,
    description: str,
) -> bool:
    """Stage and commit all changes in *worktree_path*.

    Steps:
    1. Remove ``CLAUDE.md`` from the worktree root if present (executor
       artifact that should not be committed).
    2. ``git add -A``
    3. If nothing is staged, return ``False``.
    4. Warn and skip any file larger than 5 MB.
    5. Commit with message ``bead(<bead_id>): <description[:72]>``

    Args:
        worktree_path: Absolute path to the git worktree.
        bead_id: Bead identifier used in the commit message.
        description: Short description; truncated to 72 characters.

    Returns:
        ``True`` if a commit was created, ``False`` if there was nothing to commit.
    """
    # Remove CLAUDE.md executor artifact.
    claude_md = os.path.join(worktree_path, "CLAUDE.md")
    if os.path.exists(claude_md):
        os.remove(claude_md)
        logger.debug("Removed CLAUDE.md executor artifact from %s", worktree_path)

    # Stage everything.
    await _run_git("add", "-A", cwd=worktree_path, timeout=30.0)

    # Check if there is anything to commit.
    _, status, _ = await _run_git(
        "status", "--porcelain", cwd=worktree_path, timeout=10.0
    )
    if not status:
        logger.info("Nothing to commit in worktree %s", worktree_path)
        return False

    # Warn about oversized files (> 5 MB) and unstage them.
    _5MB = 5 * 1024 * 1024
    _, staged_files, _ = await _run_git(
        "diff", "--cached", "--name-only",
        cwd=worktree_path, timeout=10.0
    )
    oversized: list[str] = []
    for rel_path in staged_files.splitlines():
        abs_path = os.path.join(worktree_path, rel_path)
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            continue
        if size > _5MB:
            logger.warning(
                "File %s is %.1f MB (> 5 MB limit); skipping from commit.",
                rel_path,
                size / (1024 * 1024),
            )
            oversized.append(rel_path)

    if oversized:
        await _run_git(
            "reset", "HEAD", "--", *oversized,
            cwd=worktree_path, timeout=10.0
        )
        # Re-check — if we unstaged everything, bail out.
        _, status, _ = await _run_git(
            "status", "--porcelain", cwd=worktree_path, timeout=10.0
        )
        if not status:
            logger.info(
                "Nothing left to commit after skipping oversized files in %s",
                worktree_path,
            )
            return False

    commit_msg = f"bead({bead_id}): {description[:72]}"
    await _run_git(
        "commit", "-m", commit_msg,
        cwd=worktree_path, timeout=30.0
    )
    logger.info("Committed worktree %s: %s", worktree_path, commit_msg)
    return True


async def merge_to_main(
    branch: str,
    bead_id: str,
    description: str,
    repo_root: str,
) -> tuple[bool, str]:
    """Merge *branch* into main with a no-fast-forward merge.

    **This function must be called while holding** :data:`_merge_lock`.  The
    caller is responsible for acquiring the lock before calling this function
    and releasing it afterwards, e.g.::

        async with git._merge_lock:
            ok, files = await git.merge_to_main(branch, bead_id, desc, root)

    Steps:
    1. Verify main is still clean.
    2. Collect the list of changed files: ``git diff --name-only main...<branch>``
    3. ``git checkout main``
    4. ``git merge --no-ff <branch> -m "bead(<bead_id>): <description[:72]>"``
    5. On conflict: ``git merge --abort``, return ``(False, changed_files_str)``
    6. On success: return ``(True, changed_files_str)``

    Args:
        branch: Name of the bead branch to merge.
        bead_id: Bead identifier for the merge commit message.
        description: Short description; truncated to 72 characters.
        repo_root: Root directory of the main repository checkout.

    Returns:
        ``(success, changed_files)`` where *changed_files* is a newline-joined
        string of file paths changed relative to main.
    """
    # Verify main is still clean (tracked files only; untracked files such as
    # worktree directories under .worktrees are intentionally ignored).
    _, status, _ = await _run_git(
        "status", "--porcelain", "-uno",
        cwd=repo_root, timeout=10.0
    )
    if status:
        raise RuntimeError(
            f"main is dirty before merge of {branch!r}. "
            "Something modified main outside of Whittler."
        )

    # Get list of changed files.
    _, changed_files, _ = await _run_git(
        "diff", "--name-only", f"main...{branch}",
        cwd=repo_root, timeout=10.0
    )

    # Switch to main.
    await _run_git("checkout", "main", cwd=repo_root, timeout=30.0)

    # Attempt merge.
    commit_msg = f"bead({bead_id}): {description[:72]}"
    rc, _, stderr = await _run_git(
        "merge", "--no-ff", branch, "-m", commit_msg,
        cwd=repo_root, timeout=60.0, check=False
    )

    if rc != 0:
        logger.warning(
            "Merge of %r into main failed (conflict). Aborting. stderr: %s",
            branch,
            stderr,
        )
        await _run_git(
            "merge", "--abort", cwd=repo_root, timeout=30.0, check=False
        )
        return False, changed_files

    logger.info("Merged branch %r into main: %s", branch, commit_msg)
    return True, changed_files


async def remove_worktree(
    worktree_path: str,
    branch: str,
    repo_root: str,
) -> None:
    """Best-effort removal of a worktree and its associated branch.

    Errors are logged at WARNING level but not re-raised.

    Args:
        worktree_path: Absolute path to the worktree directory.
        branch: Name of the bead branch to delete.
        repo_root: Root directory of the main repository checkout.
    """
    try:
        await _run_git(
            "worktree", "remove", "--force", worktree_path,
            cwd=repo_root, timeout=30.0
        )
        logger.info("Removed worktree %s", worktree_path)
    except Exception as exc:
        logger.warning("Failed to remove worktree %s: %s", worktree_path, exc)

    try:
        await _run_git(
            "branch", "-D", branch,
            cwd=repo_root, timeout=10.0
        )
        logger.info("Deleted branch %s", branch)
    except Exception as exc:
        logger.warning("Failed to delete branch %s: %s", branch, exc)


async def cleanup_stale_worktrees(
    repo_root: str,
    worktree_base: str,
) -> list[str]:
    """Remove orphaned bead worktrees that are no longer tracked by git.

    Steps:
    1. Run ``git worktree prune`` to let git clean up its own bookkeeping.
    2. Scan ``<repo_root>/<worktree_base>`` for directories named ``bead-*``.
    3. For each such directory, check whether git still knows about it as a
       live worktree; if not, forcibly remove the directory.

    Args:
        repo_root: Root directory of the main repository checkout.
        worktree_base: Sub-directory (relative to *repo_root*) that contains
            the bead worktrees.

    Returns:
        List of bead IDs (strings after the ``bead-`` prefix) that were
        cleaned up.
    """
    # Prune stale git worktree metadata.
    try:
        await _run_git("worktree", "prune", cwd=repo_root, timeout=30.0)
    except Exception as exc:
        logger.warning("git worktree prune failed: %s", exc)

    worktree_dir = os.path.join(repo_root, worktree_base)
    if not os.path.isdir(worktree_dir):
        return []

    # Get the list of worktrees currently registered with git.
    try:
        _, wt_list_output, _ = await _run_git(
            "worktree", "list", "--porcelain",
            cwd=repo_root, timeout=10.0
        )
    except Exception as exc:
        logger.warning("git worktree list failed: %s", exc)
        wt_list_output = ""

    # Extract paths of live worktrees.
    live_paths: set[str] = set()
    for line in wt_list_output.splitlines():
        if line.startswith("worktree "):
            live_paths.add(line[len("worktree "):].strip())

    cleaned: list[str] = []
    try:
        entries = os.listdir(worktree_dir)
    except OSError as exc:
        logger.warning("Cannot list worktree directory %s: %s", worktree_dir, exc)
        return cleaned

    for entry in entries:
        if not entry.startswith("bead-"):
            continue
        full_path = os.path.join(worktree_dir, entry)
        if not os.path.isdir(full_path):
            continue
        if full_path in live_paths:
            # Still a live worktree; leave it alone.
            continue

        bead_id = entry[len("bead-"):]
        logger.info(
            "Cleaning up stale bead worktree %s (bead_id=%s)", full_path, bead_id
        )
        try:
            await _run_git(
                "worktree", "remove", "--force", full_path,
                cwd=repo_root, timeout=30.0, check=False
            )
        except Exception:
            pass

        # If the directory still exists after git worktree remove, nuke it.
        if os.path.isdir(full_path):
            import shutil
            try:
                shutil.rmtree(full_path)
            except Exception as exc:
                logger.warning("Could not remove %s: %s", full_path, exc)
                continue

        cleaned.append(bead_id)

    return cleaned
