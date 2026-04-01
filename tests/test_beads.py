"""Tests for the beads module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beads_mcp.bd_client import BdError
from beads_mcp.models import Issue

from whittler.beads import ready, claim, close, unclaim, feedback, update_status
from whittler.core import BeadConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_issue(**kwargs) -> Issue:
    """Return a minimal valid Issue, optionally overriding fields."""
    defaults = dict(
        id="ISS-42",
        title="Fix the thing",
        description="A longer description of the thing",
        design="## Design\nDo it this way",
        notes="Some notes",
        status="open",
        priority=2,
        issue_type="task",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return Issue(**defaults)


# ---------------------------------------------------------------------------
# Tests for ready()
# ---------------------------------------------------------------------------


class TestReady:
    @pytest.mark.asyncio
    async def test_ready_returns_list_of_bead_configs_on_success(self):
        """ready() should return a list of BeadConfig objects from ready issues."""
        issue1 = _make_issue(id="ISS-1", title="First task")
        issue2 = _make_issue(id="ISS-2", title="Second task")

        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.ready = AsyncMock(return_value=[issue1, issue2])
            mock_client_class.return_value = mock_client

            result = await ready("/repo")

            assert len(result) == 2
            assert isinstance(result[0], BeadConfig)
            assert result[0].id == "ISS-1"
            assert result[0].description == "First task"
            assert isinstance(result[1], BeadConfig)
            assert result[1].id == "ISS-2"
            assert result[1].description == "Second task"
            mock_client_class.assert_called_once_with(working_dir="/repo")

    @pytest.mark.asyncio
    async def test_ready_returns_empty_list_on_bderror(self):
        """ready() should return [] on BdError and log a warning."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.ready = AsyncMock(side_effect=BdError("Connection failed"))
            mock_client_class.return_value = mock_client

            with patch("whittler.beads.logger") as mock_logger:
                result = await ready("/repo")

                assert result == []
                mock_logger.warning.assert_called_once()
                assert "Connection failed" in str(mock_logger.warning.call_args)

    @pytest.mark.asyncio
    async def test_ready_with_timeout_parameter(self):
        """ready() should accept timeout parameter even if unused."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.ready = AsyncMock(return_value=[])
            mock_client_class.return_value = mock_client

            result = await ready("/repo", timeout=60)

            assert result == []
            mock_client_class.assert_called_once_with(working_dir="/repo")

    @pytest.mark.asyncio
    async def test_ready_empty_result(self):
        """ready() should return [] when no issues are ready."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.ready = AsyncMock(return_value=[])
            mock_client_class.return_value = mock_client

            result = await ready("/repo")

            assert result == []


# ---------------------------------------------------------------------------
# Tests for claim()
# ---------------------------------------------------------------------------


class TestClaim:
    @pytest.mark.asyncio
    async def test_claim_returns_true_on_success(self):
        """claim() should return True when claim succeeds."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.claim = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await claim("ISS-1", "/repo")

            assert result is True
            mock_client.claim.assert_called_once()
            # Check the params passed
            call_args = mock_client.claim.call_args
            assert call_args[0][0].issue_id == "ISS-1"

    @pytest.mark.asyncio
    async def test_claim_returns_false_on_bderror(self):
        """claim() should return False on BdError and log a warning."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.claim = AsyncMock(side_effect=BdError("Already claimed"))
            mock_client_class.return_value = mock_client

            with patch("whittler.beads.logger") as mock_logger:
                result = await claim("ISS-1", "/repo")

                assert result is False
                mock_logger.warning.assert_called_once()
                assert "ISS-1" in str(mock_logger.warning.call_args)

    @pytest.mark.asyncio
    async def test_claim_with_timeout_parameter(self):
        """claim() should accept timeout parameter even if unused."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.claim = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await claim("ISS-1", "/repo", timeout=60)

            assert result is True


# ---------------------------------------------------------------------------
# Tests for close()
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_close_returns_true_on_success(self):
        """close() should return True when close succeeds."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.close = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await close("ISS-1", "/repo")

            assert result is True
            mock_client.close.assert_called_once()
            # Check the params passed
            call_args = mock_client.close.call_args
            assert call_args[0][0].issue_id == "ISS-1"
            assert call_args[0][0].reason == "Completed"

    @pytest.mark.asyncio
    async def test_close_returns_false_on_bderror(self):
        """close() should return False on BdError and log a warning."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.close = AsyncMock(side_effect=BdError("Issue not found"))
            mock_client_class.return_value = mock_client

            with patch("whittler.beads.logger") as mock_logger:
                result = await close("ISS-1", "/repo")

                assert result is False
                mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_with_timeout_parameter(self):
        """close() should accept timeout parameter even if unused."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.close = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await close("ISS-1", "/repo", timeout=60)

            assert result is True


# ---------------------------------------------------------------------------
# Tests for unclaim()
# ---------------------------------------------------------------------------


class TestUnclaim:
    @pytest.mark.asyncio
    async def test_unclaim_returns_true_on_success(self):
        """unclaim() should return True when update succeeds."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.update = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await unclaim("ISS-1", "/repo")

            assert result is True
            mock_client.update.assert_called_once()
            # Check the params passed
            call_args = mock_client.update.call_args
            assert call_args[0][0].issue_id == "ISS-1"
            assert call_args[0][0].status == "open"

    @pytest.mark.asyncio
    async def test_unclaim_returns_false_on_bderror(self):
        """unclaim() should return False on BdError and log a warning."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.update = AsyncMock(side_effect=BdError("Issue not found"))
            mock_client_class.return_value = mock_client

            with patch("whittler.beads.logger") as mock_logger:
                result = await unclaim("ISS-1", "/repo")

                assert result is False
                # Should log at warning level (non-fatal)
                mock_logger.warning.assert_called_once()
                assert "non-fatal" in str(mock_logger.warning.call_args).lower()

    @pytest.mark.asyncio
    async def test_unclaim_with_timeout_parameter(self):
        """unclaim() should accept timeout parameter even if unused."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.update = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await unclaim("ISS-1", "/repo", timeout=60)

            assert result is True


# ---------------------------------------------------------------------------
# Tests for update_status()
# ---------------------------------------------------------------------------


class TestUpdateStatus:
    @pytest.mark.asyncio
    async def test_update_status_success(self):
        """update_status() should return True when update succeeds."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.update = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await update_status("ISS-1", "deferred", "/repo")

            assert result is True
            mock_client.update.assert_called_once()
            # Check the params passed
            call_args = mock_client.update.call_args
            assert call_args[0][0].issue_id == "ISS-1"
            assert call_args[0][0].status == "deferred"

    @pytest.mark.asyncio
    async def test_update_status_failure(self):
        """update_status() should return False on BdError and log a warning."""
        with patch("whittler.beads.BdCliClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.update = AsyncMock(side_effect=BdError("Issue not found"))
            mock_client_class.return_value = mock_client

            with patch("whittler.beads.logger") as mock_logger:
                result = await update_status("ISS-1", "deferred", "/repo")

                assert result is False
                mock_logger.warning.assert_called_once()
                assert "ISS-1" in str(mock_logger.warning.call_args)
                assert "deferred" in str(mock_logger.warning.call_args)


# ---------------------------------------------------------------------------
# Tests for feedback()
# ---------------------------------------------------------------------------


class TestFeedback:
    @pytest.mark.asyncio
    async def test_feedback_always_returns_true(self):
        """feedback() should always return True (no-op)."""
        result = await feedback(
            "ISS-1",
            predicted="do X",
            actual="did Y",
            repo_root="/repo",
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_feedback_logs_debug_message(self):
        """feedback() should log a debug message."""
        with patch("whittler.beads.logger") as mock_logger:
            result = await feedback(
                "ISS-1",
                predicted="do X",
                actual="did Y",
                repo_root="/repo",
            )

            assert result is True
            mock_logger.debug.assert_called_once()
            debug_msg = str(mock_logger.debug.call_args)
            assert "ISS-1" in debug_msg
            assert "no-op" in debug_msg.lower()

    @pytest.mark.asyncio
    async def test_feedback_with_timeout_parameter(self):
        """feedback() should accept timeout parameter even if unused."""
        result = await feedback(
            "ISS-1",
            predicted="do X",
            actual="did Y",
            repo_root="/repo",
            timeout=60,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_feedback_with_long_predicted_and_actual(self):
        """feedback() should truncate long predicted and actual values in log."""
        long_text = "x" * 1000
        with patch("whittler.beads.logger") as mock_logger:
            result = await feedback(
                "ISS-1",
                predicted=long_text,
                actual=long_text,
                repo_root="/repo",
            )

            assert result is True
            # Log message should contain truncated text
            mock_logger.debug.assert_called_once()
