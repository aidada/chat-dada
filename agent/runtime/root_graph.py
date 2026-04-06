from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextvars import Token
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.constants import END, START
from langgraph.graph import StateGraph

from agent.runtime.dispatcher import build_route_payload  # DEPRECATED: Phase 4 cleanup
from agent.runtime.interaction import reset_graph_interrupt_bridge, set_graph_interrupt_bridge
from agent.platform.domain_registry import registry as domain_registry
from agent.platform.interrupts import request_interrupt
from agent.platform.state import RootState
from agent.platform.tracing import build_trace_metadata

_log = logging.getLogger("chatdada.root_graph")

Dispatcher = Callable[[str, list[str], str, str], Awaitable[Any]]


def _emit_custom(payload: dict[str, Any]) -> None:
    writer = get_stream_writer()
    writer(payload)


# ── 新 Coordinator 节点 ──────────────────────────────────────────────────────


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
    from agent.coordinator.state import CoordinatorConfig, CoordinatorState, ExecutionMode
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

    coordinator_graph = build_coordinator_graph()

    # P3: Forward explicit report_profile from caller so skills use it, not the default "".
    report_profile = str(request_payload.get("report_profile") or "")

    coordinator_input: CoordinatorState = {
        "original_goal": goal,
        "trace_id": state.get("task_id", ""),
        "config": CoordinatorConfig(report_profile=report_profile),
        "conversation_context": state.get("conversation_context") or "",
        "clarification_history": list(request_payload.get("clarification_history") or []),
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

    # P1: Restore serialised DAG state saved by execute_tasks_node on interrupt so the
    # resumed coordinator continues from where it left off instead of re-running finished tasks.
    existing_interrupt = state.get("interrupt_state") or {}
    dag_resume = existing_interrupt.get("_dag_resume_state") or {}
    if dag_resume:
        from agent.coordinator.state import Task, TaskVarEntry
        coordinator_input.update({
            "execution_mode": ExecutionMode.DAG,
            "task_dag": [Task(**t) for t in dag_resume.get("task_dag", [])],
            "completed_tasks": {k: Task(**v) for k, v in dag_resume.get("completed_tasks", {}).items()},
            "failed_tasks":    {k: Task(**v) for k, v in dag_resume.get("failed_tasks", {}).items()},
            "skill_runs":      dict(dag_resume.get("skill_runs") or {}),
            "task_vars":       {k: TaskVarEntry(**v) for k, v in dag_resume.get("task_vars", {}).items()},
            "pending_tasks":   list(dag_resume.get("pending_tasks") or []),
        })

    result = await stream_nested_graph(
        coordinator_graph,
        coordinator_input,
        config={"configurable": {"thread_id": state.get("task_id", "")}},
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
        from domain.conversations.context import ConversationContextBuilder
        from langgraph.config import get_configurable

        configurable = get_configurable()
        pool = configurable.get("pool") if configurable else None
        if pool is None:
            return {}
        builder = ConversationContextBuilder(pool)
        summary_text = f"用户: {task_text[:200]}\n助手: {final_result[:500]}"
        await builder.store.update_conversation_summary(
            conversation_id,
            summary_text,
            0,
        )
    except Exception:
        pass
    return {}


# ── 构建 Root Graph ──────────────────────────────────────────────────────────


def build_root_graph(*, dispatcher: Dispatcher, checkpointer: Any):
    graph = StateGraph(RootState)
    graph.add_node("normalize_input", normalize_input)
    graph.add_node("run_coordinator", run_coordinator)
    graph.add_node("persist_summary", persist_summary)

    graph.add_edge(START, "normalize_input")
    graph.add_edge("normalize_input", "run_coordinator")
    graph.add_edge("run_coordinator", "persist_summary")
    graph.add_edge("persist_summary", END)
    return graph.compile(checkpointer=checkpointer, name="chat_dada_root_graph")


# ── DEPRECATED: Phase 4 cleanup ─────────────────────────────────────────────
# The functions below are retained for backward compatibility during the
# Phase 1→4 migration.  They are no longer wired into the root graph.


# DEPRECATED: Phase 4 cleanup — replaced by Coordinator
def _build_clarification_prompt(state: RootState) -> dict[str, Any]:
    return {
        "content": "这个任务目标还不够明确。你更希望我直接回答、做深度研究，还是保留现有多工具流程？",
        "context": f"原始任务：{state['task_text']}",
        "placeholder": "例如：请直接做深度研究，并重点关注论文与实验。",
        "interrupt_type": "clarification",
    }


# DEPRECATED: Phase 4 cleanup — replaced by Coordinator
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


# DEPRECATED: Phase 4 cleanup — replaced by Coordinator
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


# DEPRECATED: Phase 4 cleanup — replaced by Coordinator
def _interrupt_bridge(payload: dict[str, Any]) -> str:
    return str(request_interrupt({**payload, "interrupt_type": "human_input"}))


# DEPRECATED: Phase 4 cleanup — replaced by Coordinator
async def run_general_chat(state: RootState) -> dict[str, Any]:
    from agent.runtime.dispatcher import run_general_chat_task
    from agent.runtime.task_execution import parse_step_payload

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


# DEPRECATED: Phase 4 cleanup — replaced by Coordinator
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
                    "clarification_history": state.get("request_payload", {}).get("clarification_history", []),
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


# DEPRECATED: Phase 4 cleanup — replaced by Coordinator
async def run_composite(state: RootState) -> dict[str, Any]:
    """Plan and execute a multi-capability composite task."""
    from agent.platform.task_planner import plan_task
    from agent.platform.step_runner import StepRunner

    plan = await plan_task(state["task_text"])

    base_params: dict[str, Any] = {
        "task_id": state["task_id"],
        "user_id": state.get("user_id", "anonymous"),
        "task_text": state["task_text"],
        "query": state.get("execution_task", state["task_text"]),
        "file_paths": state.get("file_paths", []),
    }

    runner = StepRunner()
    plan_result = await runner.run(plan, base_params=base_params)

    step_dicts = [
        {"step_id": r.step_id, "status": r.status, "output": r.output, "error": r.error}
        for r in plan_result.step_results
    ]

    return {
        "final_result": plan_result.final_output,
        "artifact_refs": [],
        "task_plan": {
            "steps": [
                {"id": s.id, "capability": s.capability, "params": s.params, "depends_on": s.depends_on}
                for s in plan.steps
            ]
        },
        "step_results": step_dicts,
    }


# DEPRECATED: Phase 4 cleanup — replaced by Coordinator
def select_path(state: RootState) -> str:
    return state["route_decision"]["execution_path"]


__all__ = ["build_root_graph"]
