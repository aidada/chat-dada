from __future__ import annotations

from agent_runtime.task_execution import TaskRunStore


class ConversationService:
    """对话领域服务包装。"""

    def __init__(self, store: TaskRunStore) -> None:
        self.store = store

    async def list_for_user(self, user_id: str):
        return await self.store.list_conversations(user_id)

    async def create(self, *, conversation_id: str, user_id: str, title: str):
        return await self.store.create_conversation(
            conversation_id=conversation_id,
            user_id=user_id,
            title=title,
        )

    async def get(self, conversation_id: str):
        return await self.store.get_conversation(conversation_id)

    async def update(self, conversation_id: str, **fields):
        return await self.store.update_conversation(conversation_id, **fields)

    async def delete(self, conversation_id: str):
        return await self.store.delete_conversation(conversation_id)

    async def get_entries(self, conversation_id: str):
        return await self.store.get_conversation_entries(conversation_id)
