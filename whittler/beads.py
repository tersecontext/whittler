"""Beads interface and management.

This module handles interaction with the bd CLI tool to poll for, claim,
and manage work units ("beads").
"""

import asyncio
import logging
from typing import Optional

from beads_mcp.bd_client import BdCliClient, BdError
from beads_mcp.models import (
    ClaimIssueParams,
    CloseIssueParams,
    ReadyWorkParams,
    UpdateIssueParams,
)

from whittler.core import BeadConfig

logger = logging.getLogger(__name__)


async def ready(repo_root: str, timeout: int = 30) -> list[BeadConfig]:
    """Poll for ready beads.

    Returns a list of BeadConfig objects for issues that are ready to work on
    (have no blocking dependencies). Returns an empty list on any error.

    Args:
        repo_root: Root directory of the repository for bd commands
        timeout: Timeout in seconds (currently unused; beads_mcp handles subprocess timeout)

    Returns:
        List of BeadConfig objects representing ready beads, or [] on error
    """
    try:
        client = BdCliClient(working_dir=repo_root)
        issues = await client.ready(ReadyWorkParams())
        return [BeadConfig.from_issue(issue) for issue in issues]
    except BdError as e:
        logger.warning(f"Failed to poll ready beads: {e}")
        return []


async def claim(bead_id: str, repo_root: str, timeout: int = 30) -> bool:
    """Claim a bead for exclusive work.

    Atomically transitions a bead from "ready" to "claimed" status.
    Returns True on success, False on any error.

    Args:
        bead_id: ID of the bead to claim
        repo_root: Root directory of the repository for bd commands
        timeout: Timeout in seconds (currently unused; beads_mcp handles subprocess timeout)

    Returns:
        True if claim succeeded, False otherwise
    """
    try:
        client = BdCliClient(working_dir=repo_root)
        await client.claim(ClaimIssueParams(issue_id=bead_id))
        return True
    except BdError as e:
        logger.warning(f"Failed to claim bead {bead_id}: {e}")
        return False


async def close(bead_id: str, repo_root: str, timeout: int = 30) -> bool:
    """Close a bead after successful completion.

    Closes a bead with the reason "Completed". Returns True on success,
    False on any error.

    Args:
        bead_id: ID of the bead to close
        repo_root: Root directory of the repository for bd commands
        timeout: Timeout in seconds (currently unused; beads_mcp handles subprocess timeout)

    Returns:
        True if close succeeded, False otherwise
    """
    try:
        client = BdCliClient(working_dir=repo_root)
        await client.close(CloseIssueParams(issue_id=bead_id, reason="Completed"))
        return True
    except BdError as e:
        logger.warning(f"Failed to close bead {bead_id}: {e}")
        return False


async def unclaim(bead_id: str, repo_root: str, timeout: int = 30) -> bool:
    """Unclaim a bead to release it back to the ready pool.

    This is a best-effort operation that transitions a bead back to "open" status.
    Returns True on success, False on any error. Failures are non-fatal and logged
    at warning level only.

    Args:
        bead_id: ID of the bead to unclaim
        repo_root: Root directory of the repository for bd commands
        timeout: Timeout in seconds (currently unused; beads_mcp handles subprocess timeout)

    Returns:
        True if unclaim succeeded, False otherwise
    """
    try:
        client = BdCliClient(working_dir=repo_root)
        await client.update(UpdateIssueParams(issue_id=bead_id, status="open"))
        return True
    except BdError as e:
        logger.warning(f"Failed to unclaim bead {bead_id} (non-fatal): {e}")
        return False


async def feedback(
    bead_id: str,
    predicted: str,
    actual: str,
    repo_root: str,
    timeout: int = 30,
) -> bool:
    """Send feedback about predicted vs actual work (best-effort, currently a no-op).

    Note: The beads_mcp package does not currently provide a feedback command.
    This function logs a debug message and always returns True.

    Args:
        bead_id: ID of the bead for feedback
        predicted: Predicted work/solution
        actual: Actual work/solution that occurred
        repo_root: Root directory of the repository
        timeout: Timeout in seconds (unused)

    Returns:
        True always (this is a no-op that always succeeds)
    """
    logger.debug(
        f"Feedback for bead {bead_id}: predicted={predicted[:50]}... "
        f"actual={actual[:50]}... (no-op)"
    )
    return True
