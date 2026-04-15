from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.constants import END, START
from langgraph.constants import CONFIG_KEY_CHECKPOINTER
from langgraph.config import get_config
from langgraph.graph import StateGraph

from agent.platform.interrupts import request_interrupt
from agent.platform.state import RootState



# ── Coordinator Nodes ──────────────────────────────────────────────────────


async def normalize_input(state: RootState) -> dict[str, Any]:
    return {
        "thread_id": state["task_id"],
        "artifact_refs": [],
        "interrupt_state": None,
        "ui_events": [],
    }


async def run_coordinator(state: RootState) -> dict[str, Any]:
    """Coordinator 统一执行入口 — 替代旧的多分支路由执行模式"""
    from agent.coordinator.agent import build_coordinator_graph
    from agent.coordinator.state import CoordinatorConfig, CoordinatorState
    from agent.platform.streaming import stream_nested_graph

    request_payload = state.get("request_payload") or {}
    route_payload = state.get("initial_route_payload") or {}

    # P2: Honour needs_clarification route — matches old maybe_clarify behaviour.
    # Uses root-level langgraph interrupt so task_execution.py sees a question event
    # and resumes via Command(resume=answer) (not nested_interrupt_pending restart).
    if (route_payload.get("execution_path") == "needs_clarification"
            and not request_payload.get("clarification_answer")):
        from agent.platform.interrupts import request_interrupt
        clarification_answer = request_interrupt({
            "content": "这个任务目标还不够明确。你更希望我直接回答、做深度研究，还是保留现有多工具流程？",
            "context": f"原始任务：{state.get('task_text', '')}",
            "placeholder": "例如：请直接做深度研究，并重点关注论文与实验。",
            "interrupt_type": "clarification",
        })
        # LangGraph resumes here — clarification_answer is the user's reply
        goal = (str(state.get("execution_task") or state.get("task_text") or "")
                + "\n\n用户补充：" + str(clarification_answer or ""))
    else:
        goal = str(state.get("execution_task") or state.get("task_text") or "")

    graph_config = get_config()
    configurable = graph_config.get("configurable", {}) if isinstance(graph_config, dict) else {}
    desktop_manager = configurable.get("desktop_manager")
    coordinator_graph = build_coordinator_graph(
        checkpointer=configurable.get(CONFIG_KEY_CHECKPOINTER),
    )

    # P3: Forward explicit report_profile from caller so skills use it, not the default "".
    report_profile = str(request_payload.get("report_profile") or "")

    desktop_tool_descriptors = []
    if desktop_manager is not None:
        try:
            desktop_tool_descriptors = list(
                desktop_manager.list_tool_descriptors(str(state.get("user_id", "") or ""))
            )
        except Exception:
            desktop_tool_descriptors = []

    coordinator_input: CoordinatorState = {
        "original_goal": goal,
        "trace_id": state.get("task_id", ""),
        "config": CoordinatorConfig(report_profile=report_profile),
        "conversation_context": state.get("conversation_context") or "",
        "clarification_history": list(request_payload.get("clarification_history") or []),
        "source_files": list(state.get("file_paths") or []),
        "request_user_id": str(state.get("user_id", "") or ""),
        "desktop_tool_descriptors": desktop_tool_descriptors,
        "artifact_refs": [],
        "review": {},
        "budget": {},
        "strategy_trace": [],
        "pending_tasks": [],
        "running_tasks": {},
        "completed_tasks": {},
        "failed_tasks": {},
        "skill_runs": {},
        "task_vars": {},
    }

    result = await stream_nested_graph(
        coordinator_graph,
        coordinator_input,
        config={
            "configurable": {
                "thread_id": state.get("task_id", ""),
                "checkpoint_ns": "coordinator",
                "tool_gateway": configurable.get("tool_gateway"),
                "desktop_manager": desktop_manager,
                "request_user_id": str(state.get("user_id", "") or ""),
            }
        },
        extra_payload={
            "nested_graph": "coordinator",
            "trace_id": state.get("task_id", ""),
        },
    )

    if result is None:
        result = {}

    payload: dict[str, Any] = {
        "final_result": str(result.get("final_result") or ""),
        "artifact_refs": list(result.get("artifact_refs") or []),
        "review": dict(result.get("review") or {}),
        "budget": dict(result.get("budget") or {}),
    }

    strategy_trace = result.get("strategy_trace") or []
    if strategy_trace:
        payload["research_strategy"] = " → ".join(str(s) for s in strategy_trace)

    interrupt_state = result.get("interrupt_state")
    if interrupt_state:
        payload["interrupt_state"] = interrupt_state

    latest_checkpoint_id = result.get("latest_checkpoint_id")
    if latest_checkpoint_id:
        payload["latest_checkpoint_id"] = latest_checkpoint_id

    return payload


async def persist_summary(state: RootState) -> dict[str, Any]:
    conversation_id = state.get("conversation_id", "")
    if not conversation_id:
        return {}
    final_result = state.get("final_result", "")
    task_text = state.get("task_text", "")
    if not final_result:
        return {}
    try:
        from langgraph.config import get_configurable

        configurable = get_configurable()
        conversation_service = configurable.get("conversation_service") if configurable else None
        if conversation_service is None:
            return {}
        summary_text = f"用户: {task_text[:200]}\n助手: {final_result[:500]}"
        await conversation_service.update_summary(
            conversation_id,
            summary_text,
            0,
        )
    except Exception:
        pass
    return {}


# ── 构建 Root Graph ──────────────────────────────────────────────────────────


def build_root_graph(*, checkpointer: Any):
    graph = StateGraph(RootState)
    graph.add_node("normalize_input", normalize_input)
    graph.add_node("run_coordinator", run_coordinator)
    graph.add_node("persist_summary", persist_summary)

    graph.add_edge(START, "normalize_input")
    graph.add_edge("normalize_input", "run_coordinator")
    graph.add_edge("run_coordinator", "persist_summary")
    graph.add_edge("persist_summary", END)
    return graph.compile(checkpointer=checkpointer, name="chat_dada_root_graph")


__all__ = ["build_root_graph"]
