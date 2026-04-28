"""check_agents — 检查状态：依赖 / 追问 / 循环。"""
from __future__ import annotations
from typing import Any

from agent.platform.interrupts import request_interrupt
from agent.root.scheduler import get_strategy, get_ready_agents


async def check_agents(state: dict[str, Any]) -> dict[str, Any]:
    plans = state.get("agent_plans", [])
    runs = dict(state.get("agent_runs", {}))
    strategy_name = str(state.get("scheduler_strategy", "single"))
    strategy = get_strategy(strategy_name)

    pending = [r for r in runs.values() if r.get("status") == "pending"]
    waiting = [r for r in runs.values() if r.get("status") == "waiting_for_user"]
    running = [r for r in runs.values() if r.get("status") == "running"]
    failed = [r for r in runs.values() if r.get("status") == "failed"]

    # 处理等待追问的 agent — 排队暴露唯一 pending_question
    if waiting and not state.get("pending_question"):
        ask_intents = [w.get("resume_metadata") or {"agent_id": w["agent_id"]} for w in waiting]
        active_question = ask_intents[0]
        queued = ask_intents[1:]
        answer = request_interrupt(active_question)
        agent_id = str(active_question.get("agent_id", "") or "")
        if agent_id in runs:
            runs[agent_id]["status"] = "running"
            runs[agent_id]["resume_metadata"] = {
                **dict(active_question),
                "answer": str(answer or ""),
            }
        return {
            "agent_runs": runs,
            "pending_question": active_question,
            "interrupt_state": {"queued_questions": queued} if queued else None,
            "_continue": True,
        }

    # DAG 循环：仍有就绪 agent
    if strategy.get("loop") and not waiting:
        ready = get_ready_agents(plans, runs, strategy_name)
        if ready:
            return {"agent_runs": runs, "_continue": True}

    # 仍有 pending 但无就绪（等待依赖）：也不算结束，等下次 check
    if pending and not running and not waiting:
        pass

    return {"agent_runs": runs, "_continue": False}
