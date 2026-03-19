from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from typing import Any

TaskInteractionHandler = Callable[[dict[str, Any]], Awaitable[str]]

_task_interaction_handler: ContextVar[TaskInteractionHandler | None] = ContextVar(
    "task_interaction_handler",
    default=None,
)


def set_task_interaction_handler(
    handler: TaskInteractionHandler | None,
) -> Token[TaskInteractionHandler | None]:
    return _task_interaction_handler.set(handler)


def reset_task_interaction_handler(token: Token[TaskInteractionHandler | None]) -> None:
    _task_interaction_handler.reset(token)


async def ask_user(
    question: str,
    *,
    context: str = "",
    placeholder: str = "",
) -> str | None:
    handler = _task_interaction_handler.get()
    if handler is None:
        return None

    payload: dict[str, Any] = {"content": str(question or "").strip()}
    if context.strip():
        payload["context"] = context.strip()
    if placeholder.strip():
        payload["placeholder"] = placeholder.strip()
    return await handler(payload)
