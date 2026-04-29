"""Thinking-level management via ContextVar."""

from contextvars import ContextVar
from typing import Any

from agent.brain.defaults import GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS

_thinking_level: ContextVar[str] = ContextVar("thinking_level", default="xhigh")


def set_thinking_level(level: str) -> None:
    """Set thinking level for current async context."""
    _thinking_level.set(level)


def get_thinking_level() -> str:
    """Return the thinking level for the current async context."""
    return _thinking_level.get()


def normalize_google_proxy_thinking_level(level: Any) -> str | None:
    """Normalize a thinking level value for the Google proxy provider."""
    normalized = str(level or "").strip().lower()
    if not normalized:
        return None
    if normalized in GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS:
        return normalized
    return "low"
