from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from langgraph.constants import END, START
from langgraph.graph import StateGraph

from agent.coordinator.state import (
    CoordinatorConfig,
    CoordinatorState,
    ExecutionMode,
    SkillContext,
)
from agent.platform.emit import safe_emit_progress

_log = logging.getLogger("chatdada.coordinator.agent")


def _normalize_model_hints(model_hints: Any) -> dict[str, dict[str, Any]] | None:
    if not isinstance(model_hints, dict) or not model_hints:
        return None

    normalized: dict[str, dict[str, Any]] = {}
    for role_name, hint in model_hints.items():
        if not isinstance(role_name, str) or not isinstance(hint, dict):
            continue

        role_hint: dict[str, Any] = {}
        model = hint.get("model")
        provider = hint.get("provider")
        if model is not None:
            role_hint["model"] = str(model)
        if provider is not None:
            role_hint["provider"] = str(provider)
        if role_hint:
            normalized[role_name] = role_hint

    return normalized or None


async def understand_goal_node(state: CoordinatorState) -> dict[str, Any]:
    """理解用户目标，判断执行模式"""
    from agent.coordinator.skills import skill_registry

    # P1 DAG resume: task_dag already restored from interrupt state — skip LLM
    if state.get("task_dag") and state.get("execution_mode") == ExecutionMode.DAG:
        return {
            "trace_id": state.get("trace_id") or str(uuid.uuid4()),
            "config": state.get("config") or CoordinatorConfig(),
            "available_skills": skill_registry.list_skills(),
            "model_hints": state.get("model_hints"),
        }

    from agent.coordinator.prompts import build_understand_goal_prompt
    from core.models import get_llm, response_text
    from langchain_core.messages import HumanMessage, SystemMessage

    # 初始化 trace_id
    trace_id = state.get("trace_id") or str(uuid.uuid4())
    goal = state.get("original_goal", "")

    safe_emit_progress("progress.step", {"content": "理解目标...", "node": "understand_goal", "trace_id": trace_id})

    skill_summary = skill_registry.skill_summary_for_llm()
    messages = build_understand_goal_prompt(goal, skill_summary)

    # 调用 LLM
    try:
        llm = get_llm("orchestrator")
        lc_messages = []
        for msg in messages:
            if msg["role"] == "system":
                lc_messages.append(SystemMessage(content=msg["content"]))
            else:
                lc_messages.append(HumanMessage(content=msg["content"]))

        response = await llm.ainvoke(lc_messages)
        text = response_text(response).strip()

        # 解析 JSON（处理 markdown 代码块）
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3].strip()

        parsed = json.loads(text)
        mode_str = str(parsed.get("execution_mode", "direct")).lower()

        try:
            execution_mode = ExecutionMode(mode_str)
        except ValueError:
            execution_mode = ExecutionMode.DIRECT

        result: dict[str, Any] = {
            "trace_id": trace_id,
            "execution_mode": execution_mode,
            "goal_understanding": str(parsed.get("goal_understanding", goal)),
            "skill_summary": skill_summary,
            "available_skills": skill_registry.list_skills(),
            "config": state.get("config") or CoordinatorConfig(),
            "artifact_refs": [],
            "review": {},
            "budget": {},
            "strategy_trace": [],
            "model_hints": _normalize_model_hints(parsed.get("model_hints")),
        }

        if execution_mode == ExecutionMode.SINGLE_SKILL:
            result["selected_skill"] = str(parsed.get("selected_skill") or "do_research")
            skill_input = parsed.get("skill_input") or {}
            if not skill_input.get("query"):
                skill_input["query"] = goal
            result["skill_input"] = skill_input

        safe_emit_progress("progress.step", {
            "content": f"执行模式：{execution_mode.value}",
            "node": "understand_goal",
            "trace_id": trace_id,
            "execution_mode": execution_mode.value,
        })
        return result

    except Exception as exc:
        _log.exception("understand_goal LLM failed, fallback to direct: %s", exc)
        return {
            "trace_id": trace_id,
            "execution_mode": ExecutionMode.DIRECT,
            "goal_understanding": goal,
            "skill_summary": skill_summary,
            "available_skills": skill_registry.list_skills(),
            "config": state.get("config") or CoordinatorConfig(),
            "artifact_refs": [],
            "review": {},
            "budget": {},
            "strategy_trace": [],
            "model_hints": None,
        }


async def direct_answer_node(state: CoordinatorState) -> dict[str, Any]:
    """直接回答用户（替代旧 general_chat）"""
    from agent.coordinator.prompts import build_direct_answer_prompt
    from core.models import get_llm, response_text
    from langchain_core.messages import HumanMessage, SystemMessage

    goal = state.get("original_goal", "")
    conversation_context = state.get("conversation_context") or ""
    trace_id = state.get("trace_id", "")

    safe_emit_progress("progress.step", {"content": "生成回答...", "node": "direct_answer", "trace_id": trace_id})

    messages = build_direct_answer_prompt(goal, conversation_context)

    try:
        llm = get_llm("orchestrator")
        lc_messages = []
        for msg in messages:
            if msg["role"] == "system":
                lc_messages.append(SystemMessage(content=msg["content"]))
            else:
                lc_messages.append(HumanMessage(content=msg["content"]))

        full_text = ""
        try:
            async for chunk in llm.astream(lc_messages):
                delta = response_text(chunk)
                if delta:
                    full_text += delta
                    safe_emit_progress("content.delta", {"text": delta, "content": delta, "node": "direct_answer", "trace_id": trace_id})
        except Exception:
            response = await llm.ainvoke(lc_messages)
            full_text = response_text(response)

    except Exception as exc:
        _log.exception("direct_answer failed: %s", exc)
        full_text = "抱歉，我暂时无法回答这个问题。"

    return {
        "final_result": full_text,
        "artifact_refs": [],
        "review": {},
        "budget": {},
        "strategy_trace": ["direct"],
    }


async def execute_single_skill_node(state: CoordinatorState) -> dict[str, Any]:
    """调用单一技能（替代旧单领域直连路径，零 DAG 开销）"""
    from agent.coordinator.skills import (
        _make_skill_interrupt_bridge,
        run_skill_via_adapter,
        skill_registry,
    )

    selected_skill = state.get("selected_skill") or "do_research"
    skill_input = dict(state.get("skill_input") or {})
    trace_id = state.get("trace_id", "")
    goal = state.get("original_goal", "")

    if not skill_input.get("query"):
        skill_input["query"] = goal

    safe_emit_progress("progress.step", {
        "content": f"调用技能：{selected_skill}",
        "node": "execute_single_skill",
        "trace_id": trace_id,
        "skill": selected_skill,
    })

    runner = skill_registry.get_runner(selected_skill)
    if runner is None:
        _log.warning("Skill not found: %s, fallback to direct answer", selected_skill)
        return {
            "final_result": f"技能 {selected_skill} 暂不可用。",
            "artifact_refs": [],
            "review": {},
            "budget": {},
            "strategy_trace": [selected_skill],
        }

    skill_invocation_id = f"single_{uuid.uuid4().hex[:8]}"
    coordinator_task_id = trace_id or str(uuid.uuid4())
    model_hints = _normalize_model_hints(state.get("model_hints"))

    context = SkillContext(
        coordinator_task_id=coordinator_task_id,
        skill_invocation_id=skill_invocation_id,
        skill_name=selected_skill,
        trace_id=trace_id,
        request_payload=skill_input,
        clarification_history=list(state.get("clarification_history") or []),
        request_interrupt_fn=_make_skill_interrupt_bridge(coordinator_task_id, skill_invocation_id),
    )

    # NOTE: run_skill_via_adapter always re-raises GraphInterrupt as an exception;
    # it never returns SkillResult(status="interrupted"). Any interrupt propagates
    # upward via the exception path, so an "interrupted" status check here is dead
    # code and has been removed (PRD §6.4 Bug 2).
    if model_hints:
        from agent.brain.context import clear_task_model_override, set_task_model_override

        override_token = set_task_model_override(model_hints)
    else:
        clear_task_model_override = None
        override_token = None

    try:
        result = await run_skill_via_adapter(runner, skill_input, context)
    finally:
        if clear_task_model_override is not None:
            clear_task_model_override(override_token)

    if result.status in ("error", "timeout"):
        error_msg = result.error or f"技能 {selected_skill} 执行失败（{result.status}）"
        _log.warning("Single skill %s failed with status=%s: %s", selected_skill, result.status, error_msg)
        return {
            "final_result": error_msg,
            "artifact_refs": [],
            "review": {"error": error_msg, "status": result.status},
            "budget": {},
            "strategy_trace": [selected_skill],
        }

    return {
        "final_result": str(result.result or ""),
        "artifact_refs": result.artifact_refs,
        "review": result.review,
        "budget": result.budget,
        "strategy_trace": [result.strategy or selected_skill],
        "latest_checkpoint_id": result.latest_checkpoint_id,
    }


def route_after_understand_goal(state: CoordinatorState) -> str:
    """条件路由：根据 execution_mode 决定下一节点"""
    mode = state.get("execution_mode")
    if mode == ExecutionMode.DIRECT:
        return "direct"
    elif mode == ExecutionMode.SINGLE_SKILL:
        return "single_skill"
    else:
        return "dag"


def build_coordinator_graph(checkpointer=None):
    """构建 Coordinator LangGraph StateGraph。

    Args:
        checkpointer: 外部注入的持久化 checkpointer。
                      如果为 None，回退到 MemorySaver（仅用于测试）。
    """
    from agent.coordinator.executor import (
        assign_skills_node,
        check_dependencies_node,
        decompose_tasks_node,
        execute_tasks_node,
        handle_task_result_node,
        route_after_check_dependencies,
        synthesize_node,
    )

    graph = StateGraph(CoordinatorState)

    # 节点
    graph.add_node("understand_goal", understand_goal_node)
    graph.add_node("direct_answer", direct_answer_node)
    graph.add_node("execute_single_skill", execute_single_skill_node)
    graph.add_node("decompose_tasks", decompose_tasks_node)
    graph.add_node("assign_skills", assign_skills_node)
    graph.add_node("execute_tasks", execute_tasks_node)
    graph.add_node("handle_task_result", handle_task_result_node)
    graph.add_node("check_dependencies", check_dependencies_node)
    graph.add_node("synthesize", synthesize_node)

    # 边
    graph.add_edge(START, "understand_goal")
    graph.add_conditional_edges(
        "understand_goal",
        route_after_understand_goal,
        {
            "direct": "direct_answer",
            "single_skill": "execute_single_skill",
            "dag": "decompose_tasks",
        },
    )
    graph.add_edge("direct_answer", END)
    graph.add_edge("execute_single_skill", END)
    graph.add_edge("decompose_tasks", "assign_skills")
    graph.add_edge("assign_skills", "execute_tasks")
    graph.add_edge("execute_tasks", "handle_task_result")
    graph.add_edge("handle_task_result", "check_dependencies")
    graph.add_conditional_edges(
        "check_dependencies",
        route_after_check_dependencies,
        {
            "execute_tasks": "execute_tasks",
            "synthesize": "synthesize",
        },
    )
    graph.add_edge("synthesize", END)

    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()

    return graph.compile(checkpointer=checkpointer, name="coordinator_graph")


__all__ = [
    "understand_goal_node",
    "direct_answer_node",
    "execute_single_skill_node",
    "route_after_understand_goal",
    "build_coordinator_graph",
]
