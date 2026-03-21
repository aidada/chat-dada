from __future__ import annotations

from typing import Any

from langgraph.types import interrupt


def request_interrupt(payload: dict[str, Any]) -> Any:
    """Raise a LangGraph interrupt carrying a user-facing payload."""
    return interrupt(payload)

