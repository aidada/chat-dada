from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from typing import Any

TaskInteractionHandler = Callable[[dict[str, Any]], Awaitable[str]]
GraphInterruptBridge = Callable[[dict[str, Any]], str | Awaitable[str]]

_task_interaction_handler: ContextVar[TaskInteractionHandler | None] = ContextVar(
    "task_interaction_handler",
    default=None,
)
_graph_interrupt_bridge: ContextVar[GraphInterruptBridge | None] = ContextVar(
    "graph_interrupt_bridge",
    default=None,
)


def set_task_interaction_handler(
    handler: TaskInteractionHandler | None,
) -> Token[TaskInteractionHandler | None]:
    return _task_interaction_handler.set(handler)


def reset_task_interaction_handler(token: Token[TaskInteractionHandler | None]) -> None:
    _task_interaction_handler.reset(token)


def set_graph_interrupt_bridge(
    bridge: GraphInterruptBridge | None,
) -> Token[GraphInterruptBridge | None]:
    return _graph_interrupt_bridge.set(bridge)


def reset_graph_interrupt_bridge(token: Token[GraphInterruptBridge | None]) -> None:
    _graph_interrupt_bridge.reset(token)


async def ask_user(
    question: str,
    *,
    context: str = "",
    placeholder: str = "",
) -> str | None:
    bridge = _graph_interrupt_bridge.get()
    payload: dict[str, Any] = {"content": str(question or "").strip()}
    if context.strip():
        payload["context"] = context.strip()
    if placeholder.strip():
        payload["placeholder"] = placeholder.strip()

    if bridge is not None:
        result = bridge(payload)
        if hasattr(result, "__await__"):
            return await result
        return result

    handler = _task_interaction_handler.get()
    if handler is None:
        return None

    return await handler(payload)
