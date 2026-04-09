from __future__ import annotations

from fastapi import Depends

from web import runtime as web_runtime
from domain.conversations.services import ConversationService
from domain.tasks.services import TaskExecutionService


async def get_task_execution_service() -> TaskExecutionService:
    return TaskExecutionService(web_runtime.task_service)


async def get_conversation_service() -> ConversationService:
    return ConversationService(web_runtime.task_service.store)
