"""Shared helpers for provider adapters."""

import json
from typing import Any


def debug_body_preview(body: Any, limit: int = 4000) -> str:
    """Format a request or response body for debug logging."""
    if body is None:
        return "<empty>"
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    elif isinstance(body, (dict, list, tuple)):
        try:
            text = json.dumps(body, ensure_ascii=False, default=str)
        except TypeError:
            text = str(body)
    elif hasattr(body, "model_dump"):
        try:
            text = json.dumps(body.model_dump(), ensure_ascii=False, default=str)
        except TypeError:
            text = str(body)
    else:
        text = str(body)
    if not text.strip():
        return "<empty>"
    return text[:limit] + ("...(truncated)" if len(text) > limit else "")
