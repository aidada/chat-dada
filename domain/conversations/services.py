from __future__ import annotations

from typing import Any

from domain.conversations.context import generate_embeddings_async
from infra.db.repositories.conversation_repo import ConversationRepository
from infra.db.session import SessionFactory


class ConversationService:
    """对话领域的独立服务，与 Task 管理彻底解耦。"""

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            return await repo.list_for_user(user_id)

    async def create(
        self, *, conversation_id: str, user_id: str, title: str = "新对话"
    ) -> dict[str, Any]:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            row = await repo.create(
                conversation_id=conversation_id, user_id=user_id, title=title
            )
            await session.commit()
        return {
            "id": row.id,
            "user_id": row.user_id,
            "title": row.title,
            "pinned": row.pinned,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    async def get(self, conversation_id: str) -> dict[str, Any] | None:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            row = await repo.get(conversation_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "user_id": row.user_id,
            "title": row.title,
            "pinned": row.pinned,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    async def update(
        self, conversation_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            row = await repo.get(conversation_id)
            if row is None:
                return None
            row = await repo.update(row, **fields)
            await session.commit()
        return {
            "id": row.id,
            "user_id": row.user_id,
            "title": row.title,
            "pinned": row.pinned,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    async def delete(self, conversation_id: str) -> bool:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            row = await repo.get(conversation_id)
            if row is None:
                return False
            await repo.delete(row)
            await session.commit()
            return True

    async def get_entries(self, conversation_id: str) -> list[dict[str, Any]]:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            return await repo.get_entries(conversation_id)

    async def get_summary(self, conversation_id: str) -> tuple[str, int]:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            return await repo.get_summary(conversation_id)

    async def update_summary(
        self, conversation_id: str, summary: str, through_seq: int
    ) -> None:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            await repo.update_summary(conversation_id, summary, through_seq)
            await session.commit()

    async def get_primary_events(
        self, conversation_id: str, after_seq: int = 0
    ) -> list[dict[str, Any]]:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            return await repo.get_primary_events(conversation_id, after_seq)


class ConversationEmbeddingService:
    """Conversation-adjacent async jobs backed by repositories."""

    def __init__(self, session_factory=SessionFactory) -> None:
        self._session_factory = session_factory

    async def generate_embeddings(self, task_id: str) -> None:
        await generate_embeddings_async(self._session_factory, task_id)
