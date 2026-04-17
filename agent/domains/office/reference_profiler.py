from __future__ import annotations

from typing import Any


def profile_reference_payload(*, format_name: str, inspect_payload: dict[str, Any]) -> dict[str, Any]:
    if format_name == "pptx":
        outline = inspect_payload.get("outline", [])
        stats = inspect_payload.get("stats", {})
        return {
            "structure": {"units": [{"name": str(item.get("title", "") or "")} for item in outline]},
            "style": {"style_tokens": {"slide_count": int(stats.get("slide_count", 0) or 0)}},
        }
    return {
        "structure": {"units": []},
        "style": {"style_tokens": {}},
    }
