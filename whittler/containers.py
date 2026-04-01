"""
Docker container management for Whittler.

This module handles spawning, managing, and monitoring Docker containers
that run Claude Code solver agents.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any

import docker
import docker.errors

from whittler.core import BeadConfig, WhittlerConfig


class ContainerManager:
    """Manages Docker containers for Whittler solver agents."""

    def __init__(self, config: WhittlerConfig) -> None:
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as exc:
            raise RuntimeError(
                f"Cannot connect to Docker daemon. Is Docker running? ({exc})"
            ) from exc

        try:
            self.client.images.get(config.container_image)
        except docker.errors.ImageNotFound:
            raise RuntimeError(
                f"Docker image '{config.container_image}' not found. "
                f"Build or pull the image before starting Whittler."
            ) from None
        except docker.errors.APIError as exc:
            raise RuntimeError(
                f"Docker API error while checking image '{config.container_image}': {exc}"
            ) from exc

        self.config = config
        self._temp_files: dict[str, str] = {}  # Maps container_id -> temp_file_path

    # ------------------------------------------------------------------
    # Public async interface
    # ------------------------------------------------------------------

    async def spawn(self, bead: BeadConfig, worktree_path: str) -> str:
        """Spawn a container to solve a bead. Returns container ID."""
        bead_data = {
            "id": bead.id,
            "description": bead.description,
            "body": bead.body,
            "design": bead.design,
            "notes": bead.notes,
            "acceptance_criteria": bead.acceptance_criteria,
        }

        # Write bead config to a temp file that persists for the lifetime of the container.
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        json.dump(bead_data, tmp)
        tmp.flush()
        tmp.close()
        bead_config_path = tmp.name

        volumes = {
            worktree_path: {"bind": "/work", "mode": "rw"},
            bead_config_path: {"bind": "/bead.json", "mode": "ro"},
        }
        environment = {
            self.config.api_key_env: os.environ.get(self.config.api_key_env, ""),
            "WHITTLER_MAX_RETRIES": str(self.config.max_retries),
            "WHITTLER_VALIDATION_CMD": self.config.validation_command,
        }

        def _run() -> Any:
            return self.client.containers.run(
                image=self.config.container_image,
                volumes=volumes,
                environment=environment,
                mem_limit=self.config.container_memory,
                nano_cpus=int(self.config.container_cpu * 1_000_000_000),
                labels={"whittler": "true", "whittler.bead_id": bead.id},
                detach=True,
                auto_remove=False,
            )

        container = await asyncio.get_running_loop().run_in_executor(None, _run)
        self._temp_files[container.id] = bead_config_path
        return container.id

    async def wait(self, container_id: str, timeout: int) -> int:
        """Wait for container to exit. Returns exit code. Returns -1 on timeout."""
        def _wait() -> dict:
            c = self.client.containers.get(container_id)
            return c.wait()

        try:
            result = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _wait),
                timeout=timeout,
            )
            return result["StatusCode"]
        except asyncio.TimeoutError:
            # Best-effort kill
            await self.kill(container_id)
            return -1

    async def logs(self, container_id: str) -> str:
        """Return container stdout+stderr. Truncated to last 10000 chars."""
        def _logs() -> bytes:
            c = self.client.containers.get(container_id)
            return c.logs(stdout=True, stderr=True)

        raw: bytes = await asyncio.get_running_loop().run_in_executor(None, _logs)
        text = raw.decode("utf-8", errors="replace")
        return text[-10000:]

    async def kill(self, container_id: str) -> None:
        """Force kill container. Best-effort (ignores NotFound, APIError)."""
        def _kill() -> None:
            try:
                c = self.client.containers.get(container_id)
                c.kill()
            except (docker.errors.NotFound, docker.errors.APIError):
                pass

        await asyncio.get_running_loop().run_in_executor(None, _kill)

    async def cleanup(self, container_id: str) -> None:
        """Remove container. Best-effort."""
        def _remove() -> None:
            try:
                c = self.client.containers.get(container_id)
                c.remove(force=True)
            except (docker.errors.NotFound, docker.errors.APIError):
                pass

        await asyncio.get_running_loop().run_in_executor(None, _remove)

        # Clean up temp file if one was created for this container
        tmp = self._temp_files.pop(container_id, None)
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)

    async def cleanup_orphans(self, label: str = "whittler") -> int:
        """Find and remove stopped containers with label=true. Returns count."""
        def _cleanup() -> int:
            containers = self.client.containers.list(
                all=True,
                filters={"label": f"{label}=true", "status": "exited"},
            )
            count = 0
            for c in containers:
                try:
                    c.remove(force=True)
                    count += 1
                except (docker.errors.NotFound, docker.errors.APIError):
                    pass
            return count

        return await asyncio.get_running_loop().run_in_executor(None, _cleanup)
