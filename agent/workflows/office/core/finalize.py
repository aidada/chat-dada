from __future__ import annotations

from typing import Any

from agent.runtime.cost_logging import append_stage_record


async def finalize_node(state: dict[str, Any]) -> dict[str, Any]:
    cost_ledger = append_stage_record(
        dict(state.get("cost_ledger") or {}),
        stage="finalize",
        status="ready",
        elapsed_ms=0,
        metadata={
            "completed_pages": int(state.get("completed_pages", 0) or 0),
            "terminal_status": str(state.get("terminal_status", "") or ""),
        },
    )
    final_result = str(state.get("final_result", "") or "")
    if not final_result:
        results = state.get("intermediate_results", [])
        if results:
            final_result = str(results[-1].get("output", "") or "")
    return {
        "current_stage": "finalize",
        "final_result": final_result,
        "cost_ledger": cost_ledger,
        "partial_progress": dict(state.get("partial_progress") or {}),
    }
