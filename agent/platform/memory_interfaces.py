"""Abstract interfaces for memory providers used by the task platform.

These are ABCs only — implementations live in concrete backends (e.g., Postgres, Redis).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class UserMemoryProvider(ABC):
    @abstractmethod
    async def get_user_context(self, user_id: str) -> dict[str, Any]:
        """Return contextual data for a user (preferences, history summary, etc.)."""

    @abstractmethod
    async def update_user_context(self, user_id: str, patch: dict[str, Any]) -> None:
        """Merge *patch* into the stored user context."""


class ThreadMemoryProvider(ABC):
    @abstractmethod
    async def get_thread_context(self, thread_id: str) -> dict[str, Any]:
        """Return the conversation/thread context for the given thread."""

    @abstractmethod
    async def save_thread_summary(self, thread_id: str, summary: str) -> None:
        """Persist a rolling summary for the thread."""


class CheckpointProvider(ABC):
    @abstractmethod
    async def get_checkpoint(self, thread_id: str, checkpoint_id: str) -> dict[str, Any] | None:
        """Retrieve a specific checkpoint by ID."""

    @abstractmethod
    async def list_checkpoints(self, thread_id: str) -> list[dict[str, Any]]:
        """List all checkpoints for a thread, ordered chronologically."""
