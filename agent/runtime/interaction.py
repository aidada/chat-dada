from __future__ import annotations

from collections import deque
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
_preloaded_user_replies: ContextVar[deque[Any] | None] = ContextVar(
    "preloaded_user_replies",
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


def set_preloaded_user_replies(
    replies: list[Any] | None,
) -> Token[deque[Any] | None]:
    normalized: deque[Any] = deque()
    for item in replies or []:
        if isinstance(item, dict):
            answer = str(item.get("answer", "") or "").strip()
            if answer:
                normalized.append(
                    {
                        "question": str(item.get("question", item.get("content", "")) or "").strip(),
                        "answer": answer,
                    }
                )
            continue
        text = str(item).strip()
        if text:
            normalized.append(text)
    return _preloaded_user_replies.set(normalized or None)


def reset_preloaded_user_replies(token: Token[deque[Any] | None]) -> None:
    _preloaded_user_replies.reset(token)


def _normalize_question(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _pop_preloaded_reply(
    queued_replies: deque[Any],
    question: str,
) -> str | None:
    if not queued_replies:
        return None

    first = queued_replies[0]
    if not isinstance(first, dict):
        return str(queued_replies.popleft())

    normalized_question = _normalize_question(question)
    for item in list(queued_replies):
        if not isinstance(item, dict):
            continue
        stored_question = _normalize_question(str(item.get("question", "") or ""))
        if stored_question and stored_question == normalized_question:
            queued_replies.remove(item)
            return str(item.get("answer", "") or "")
    return None


async def ask_user(
    question: str,
    *,
    context: str = "",
    placeholder: str = "",
) -> str | None:
    queued_replies = _preloaded_user_replies.get()
    if queued_replies:
        preloaded = _pop_preloaded_reply(queued_replies, question)
        if preloaded is not None:
            return preloaded

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


__all__ = [
    "ask_user",
    "reset_preloaded_user_replies",
    "reset_graph_interrupt_bridge",
    "reset_task_interaction_handler",
    "set_preloaded_user_replies",
    "set_graph_interrupt_bridge",
    "set_task_interaction_handler",
]
