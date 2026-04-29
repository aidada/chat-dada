"""finalize — Root Graph 出口。

Root Graph 只返回 final_result / artifact_refs / review / budget。
TaskService._execute_task 仍然是唯一的任务结果持久化和 lifecycle.completed 事件出口。
"""
from __future__ import annotations
from typing import Any


async def finalize(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "final_result": str(state.get("final_result", "") or ""),
        "artifact_refs": list(state.get("artifact_refs", []) or []),
        "review": dict(state.get("review", {}) or {}),
        "budget": dict(state.get("budget", {}) or {}),
        "strategy_trace": list(state.get("strategy_trace", []) or []),
    }
