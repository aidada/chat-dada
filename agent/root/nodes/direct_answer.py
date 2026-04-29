"""direct_answer — 直接回答，不创建 agent。"""
from __future__ import annotations
from typing import Any

from agent.platform.emit import safe_emit_progress
from core.models import get_llm, response_text

async def direct_answer(state: dict[str, Any]) -> dict[str, Any]:
    goal = str(state.get("original_goal", "") or state.get("task_text", ""))
    trace_id = str(state.get("task_id", ""))

    safe_emit_progress("progress.step", {
        "content": "生成回答...",
        "node": "direct_answer",
        "trace_id": trace_id,
    })

    try:
        from langchain_core.messages import HumanMessage
        llm = get_llm("orchestrator")
        response = await llm.ainvoke([HumanMessage(content=goal)])
        full_text = response_text(response)
    except Exception:
        full_text = "抱歉，我暂时无法回答这个问题。"

    return {
        "final_result": full_text or "抱歉，我暂时无法回答这个问题。",
        "strategy_trace": ["direct"],
    }
