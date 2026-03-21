from __future__ import annotations

from typing import Any


def build_trace_metadata(
    *,
    task_id: str,
    user_id: str,
    mode: str,
    route_name: str,
    domain: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "user_id": user_id,
        "mode": mode,
        "route_name": route_name,
        "domain": domain,
    }

