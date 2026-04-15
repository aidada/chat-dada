"""LangSmith tracing: runtime toggle, connection verification, run metadata."""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("chatdada.langsmith")


# ── Runtime toggle ──────────────────────────────────────────────────────────


def is_langsmith_enabled() -> bool:
    return os.environ.get("LANGSMITH_TRACING", "").lower() == "true"


def set_langsmith_enabled(enabled: bool) -> None:
    os.environ["LANGSMITH_TRACING"] = "true" if enabled else "false"
    log.info("LangSmith tracing set to %s", enabled)


# ── Connection verification ─────────────────────────────────────────────────


def verify_langsmith_connection() -> dict[str, Any]:
    """Check LangSmith connectivity. Returns status dict (never raises)."""
    if not is_langsmith_enabled():
        return {"ok": False, "reason": "tracing_disabled"}

    api_key = os.environ.get("LANGSMITH_API_KEY", "")
    if not api_key:
        return {"ok": False, "reason": "missing_api_key"}

    try:
        from langsmith import Client

        client = Client()
        info = client.info
        return {
            "ok": True,
            "project": os.environ.get("LANGSMITH_PROJECT", ""),
            "endpoint": os.environ.get("LANGSMITH_ENDPOINT", ""),
            "instance_flags": info,
        }
    except Exception as exc:
        return {"ok": False, "reason": "connection_failed", "error": str(exc)}


# ── Run metadata builder ────────────────────────────────────────────────────


def build_langsmith_run_config(
    *,
    task_id: str,
    user_id: str,
    domain: str,
    mode: str,
) -> dict[str, Any]:
    """Return a config dict to merge into the LangGraph invoke/astream config."""
    if not is_langsmith_enabled():
        return {}

    return {
        "metadata": {
            "task_id": task_id,
            "user_id": user_id,
            "domain": domain,
            "mode": mode,
        },
        "tags": [f"domain:{domain}", f"mode:{mode}"],
        "run_name": f"{domain}/{task_id}",
    }
