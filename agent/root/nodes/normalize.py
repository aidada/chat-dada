"""normalize_input — 整理初始状态。"""
from __future__ import annotations
from typing import Any

async def normalize_input(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_refs": [],
        "interrupt_state": None,
        "pending_question": None,
        "agent_runs": {},
        "task_vars": {},
        "strategy_trace": [],
    }
