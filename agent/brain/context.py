"""Per-task model override via ContextVar."""

from contextvars import ContextVar
from contextvars import Token
from typing import Any

_task_model_override: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(
    "task_model_override",
    default=None,
)


def set_task_model_override(overrides: dict[str, dict[str, Any]]) -> Token[dict[str, dict[str, Any]] | None]:
    """Set per-task model overrides keyed by role for the current async context."""
    return _task_model_override.set(overrides)


def get_task_model_override() -> dict[str, dict[str, Any]] | None:
    """Return the current role-keyed per-task overrides."""
    return _task_model_override.get()


def clear_task_model_override(token: Token[dict[str, dict[str, Any]] | None] | None = None) -> None:
    """Clear per-task model overrides for the current async context."""
    if token is not None:
        _task_model_override.reset(token)
        return
    _task_model_override.set(None)
