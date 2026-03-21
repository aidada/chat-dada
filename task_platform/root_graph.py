from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import Token
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.constants import END, START
from langgraph.graph import StateGraph

from task_platform.domain_registry import registry as domain_registry
from task_platform.interrupts import request_interrupt
from task_platform.router import build_route_payload
from task_platform.state import RootState
from task_platform.tracing import build_trace_metadata
from runtime.task_interaction import reset_graph_interrupt_bridge, set_graph_interrupt_bridge


Dispatcher = Callable[[str, list[str], str, str], Awaitable[Any]]


def _emit_custom(payload: dict[str, Any]) -> None:
    writer = get_stream_writer()
    writer(payload)


def _build_clarification_prompt(state: RootState) -> dict[str, Any]:
    return {
        "content": "这个任务目标还不够明确。你更希望我直接回答、做深度研究，还是保留现有多工具流程？",
        "context": f"原始任务：{state['task_text']}",
        "placeholder": "例如：请直接做深度研究，并重点关注论文与实验。",
        "interrupt_type": "clarification",
    }


async def normalize_input(state: RootState) -> dict[str, Any]:
    return {
        "thread_id": state["task_id"],
        "artifact_refs": [],
        "interrupt_state": None,
        "ui_events": [],
    }


def make_route_domain(dispatcher: Dispatcher):
    async def route_domain(state: RootState) -> dict[str, Any]:
        route = state.get("initial_route_payload")
        decision = None
        if route is None:
            decision = await dispatcher(
                state["task_text"],
                state.get("file_paths", []),
                state.get("mode", "auto"),
                state.get("user_id", "anonymous"),
            )
            route = build_route_payload(
                task_text=state["task_text"],
                file_paths=state.get("file_paths", []),
                decision=decision,
            )
        clarification_answer = str(state.get("request_payload", {}).get("clarification_answer", "") or "").strip()
        if clarification_answer and route["execution_path"] == "needs_clarification":
            enriched_text = f"{state['task_text']}\n\n用户补充：{clarification_answer}"
            route = build_route_payload(
                task_text=enriched_text,
                file_paths=state.get("file_paths", []),
                decision=decision,
            )
            if route["execution_path"] == "needs_clarification":
                route["route_name"] = "research"
                route["execution_path"] = "research"
                route["reason"] = f"{route['reason']}; clarification answer provided"
        domain = route["execution_path"]
        return {
            "route_decision": route,
            "route_name": route["route_name"],
            "route_reason": route["reason"],
            "route_confidence": route["confidence"],
            "domain": domain,
            "trace_metadata": build_trace_metadata(
                task_id=state["task_id"],
                user_id=state["user_id"],
                mode=state["mode"],
                route_name=route["route_name"],
                domain=domain,
            ),
        }

    return route_domain


async def maybe_clarify(state: RootState) -> dict[str, Any]:
    if state["route_decision"]["execution_path"] != "needs_clarification":
        return {}
    payload = _build_clarification_prompt(state)
    answer = request_interrupt(payload)
    return {
        "interrupt_state": None,
        "pending_question": None,
        "request_payload": {**state["request_payload"], "clarification_answer": str(answer)},
    }


def _interrupt_bridge(payload: dict[str, Any]) -> str:
    return str(request_interrupt({**payload, "interrupt_type": "human_input"}))


async def run_general_chat(state: RootState) -> dict[str, Any]:
    from runtime.task_dispatcher import run_general_chat_task
    from runtime.task_runtime import parse_step_payload

    async def on_step(step_info: str) -> None:
        event_type, payload = parse_step_payload(step_info)
        payload["event_type"] = event_type
        _emit_custom(payload)

    result = await run_general_chat_task(
        state["execution_task"],
        on_step,
        user_id=state["user_id"],
        conversation_context=state.get("conversation_context", ""),
    )
    return {
        "final_result": result,
        "artifact_refs": [],
    }


def make_run_registered_domain(domain_name: str, *, enable_interrupt_bridge: bool = False):
    async def run_registered_domain(state: RootState) -> dict[str, Any]:
        runner = domain_registry.get(domain_name)
        if runner is None:
            raise RuntimeError(f"Domain runner not registered: {domain_name}")

        token: Token[Any] | None = None
        if enable_interrupt_bridge:
            token = set_graph_interrupt_bridge(_interrupt_bridge)
        try:
            result = await runner(
                {
                    "task_id": state["task_id"],
                    "query": state["execution_task"],
                    "task": state["task_text"],
                    "report_profile": state.get("request_payload", {}).get("report_profile", ""),
                    "parallel": domain_name == "research",
                    "use_deepagents": domain_name == "research",
                    "browser_enabled": False,
                }
            )
        finally:
            if token is not None:
                reset_graph_interrupt_bridge(token)

        payload = {
            "final_result": result.result,
            "artifact_refs": result.artifact_refs,
            "review": getattr(result, "review", {}),
            "budget": getattr(result, "budget", {}),
        }
        strategy = getattr(result, "strategy", "")
        if strategy:
            payload["research_strategy"] = strategy
        return payload

    return run_registered_domain


async def persist_summary(state: RootState) -> dict[str, Any]:
    """Persist a rolling conversation summary for multi-turn context."""
    conversation_id = state.get("conversation_id", "")
    if not conversation_id:
        return {}
    final_result = state.get("final_result", "")
    task_text = state.get("task_text", "")
    if not final_result:
        return {}
    try:
        from runtime.conversation_context import ConversationContextBuilder
        from langgraph.config import get_configurable

        configurable = get_configurable()
        pool = configurable.get("pool") if configurable else None
        if pool is None:
            return {}
        builder = ConversationContextBuilder(pool)
        summary_text = f"用户: {task_text[:200]}\n助手: {final_result[:500]}"
        await builder.store.update_conversation_summary(
            conversation_id, summary_text, 0,
        )
    except Exception:
        pass
    return {}


def select_path(state: RootState) -> str:
    return state["route_decision"]["execution_path"]


def build_root_graph(*, dispatcher: Dispatcher, checkpointer: Any):
    graph = StateGraph(RootState)
    graph.add_node("normalize_input", normalize_input)
    graph.add_node("route_domain", make_route_domain(dispatcher))
    graph.add_node("maybe_clarify", maybe_clarify)
    graph.add_node("run_general_chat", run_general_chat)
    graph.add_node("run_research", make_run_registered_domain("research", enable_interrupt_bridge=True))
    graph.add_node("run_patent", make_run_registered_domain("patent", enable_interrupt_bridge=True))
    graph.add_node("run_zero_report", make_run_registered_domain("zero_report", enable_interrupt_bridge=True))
    graph.add_node("run_ppt", make_run_registered_domain("ppt", enable_interrupt_bridge=True))
    graph.add_node("persist_summary", persist_summary)

    graph.add_edge(START, "normalize_input")
    graph.add_edge("normalize_input", "route_domain")
    graph.add_conditional_edges(
        "route_domain",
        select_path,
        {
            "general_chat": "run_general_chat",
            "research": "run_research",
            "patent": "run_patent",
            "zero_report": "run_zero_report",
            "ppt": "run_ppt",
            "needs_clarification": "maybe_clarify",
        },
    )
    graph.add_edge("maybe_clarify", "route_domain")
    graph.add_edge("run_general_chat", "persist_summary")
    graph.add_edge("run_research", "persist_summary")
    graph.add_edge("run_patent", "persist_summary")
    graph.add_edge("run_zero_report", "persist_summary")
    graph.add_edge("run_ppt", "persist_summary")
    graph.add_edge("persist_summary", END)
    return graph.compile(checkpointer=checkpointer, name="chat_dada_root_graph")
