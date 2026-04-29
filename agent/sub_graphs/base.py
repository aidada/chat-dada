"""通用 ReAct Graph 构建器。

所有 agent_type 共用同一套 ReAct 图结构：
    prepare_context → reason → select_skill → load_guidance → decide_action
                         ↑
                         └── observe ←── call_tool / ask_user
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from langgraph.constants import END, START
from langgraph.config import get_config
from langgraph.graph import StateGraph

from agent.hands.protocol import ToolCall, ToolContext, ToolResult
from agent.platform.emit import safe_emit_progress
from agent.sub_graphs.state import AgentState
from core.models import get_llm, response_text

_log = logging.getLogger("chatdada.sub_graphs")


def _extract_llm_decision(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"reasoning": text[:200], "action": "finalize",
                "draft_update": text}


async def prepare_context(state: AgentState) -> dict[str, Any]:
    from langgraph.config import get_config
    cfg = get_config()
    configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
    skill_loader = configurable.get("skill_loader")
    tool_gateway = configurable.get("tool_gateway")
    skill_context = configurable.get("skill_context")

    skill_candidates = []
    if skill_loader and skill_context:
        skill_candidates = skill_loader.search(
            state.goal,
            domain=skill_context.skill_domain,
            hints=skill_context.skill_hints,
            top_k=3,
        )

    tools_desc = ""
    if tool_gateway and skill_context:
        tools_desc = tool_gateway.describe(skill_context.allowed_tool_names)

    skill_summary = skill_loader.summarize(skill_candidates) if skill_loader else ""

    system_msg = (
        f"## 目标\n{state.goal}\n\n"
        f"{skill_summary}\n\n"
        f"{tools_desc}\n\n"
        "## 指令\n"
        "分析当前目标，推理下一步行动。输出 JSON：\n"
        "{\"reasoning\": \"...\", \"selected_skill\": null 或 \"skill_name:v1\", "
        "\"action\": \"call_tool\"|\"ask_user\"|\"finalize\", "
        "\"tool_calls\": [{\"name\": \"...\", \"args\": {...}}], "
        "\"user_question\": \"...\", \"draft_update\": \"...\"}"
    )
    return {"messages": [{"role": "system", "content": system_msg}]}


async def reason(state: AgentState) -> dict[str, Any]:
    llm = get_llm("orchestrator")
    lc_messages = []
    for msg in state.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            from langchain_core.messages import SystemMessage
            lc_messages.append(SystemMessage(content=content))
        elif role == "assistant":
            from langchain_core.messages import AIMessage
            lc_messages.append(AIMessage(content=content))
        else:
            from langchain_core.messages import HumanMessage
            lc_messages.append(HumanMessage(content=content))

    response = await llm.ainvoke(lc_messages)
    text = response_text(response)
    decision = _extract_llm_decision(text)

    safe_emit_progress("progress.step", {
        "content": f"推理: {str(decision.get('reasoning', ''))[:120]}",
        "agent_id": state.agent_id,
        "iteration": state.iteration + 1,
    })

    return {
        "messages": state.messages + [{"role": "assistant", "content": text}],
        "decision_summary": str(decision.get("reasoning", ""))[:500],
        "selected_skill": decision.get("selected_skill"),
        "action": decision.get("action"),
        "draft_result": str(decision.get("draft_update", state.draft_result or "")),
        "iteration": state.iteration + 1,
    }


async def select_skill(state: AgentState) -> dict[str, Any]:
    return {}


async def load_guidance(state: AgentState) -> dict[str, Any]:
    if not state.selected_skill:
        return {}
    from langgraph.config import get_config
    cfg = get_config()
    configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
    skill_loader = configurable.get("skill_loader")

    if skill_loader:
        guidance = skill_loader.load_guidance(state.selected_skill)
        if guidance:
            return {
                "messages": state.messages + [
                    {"role": "system", "content": guidance}
                ],
                "strategy_trace": state.strategy_trace + [state.selected_skill],
            }
    return {}


async def decide_action(state: AgentState) -> dict[str, Any]:
    return {}


async def call_tool(state: AgentState) -> dict[str, Any]:
    from langgraph.config import get_config
    cfg = get_config()
    configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
    tool_gateway = configurable.get("tool_gateway")
    skill_context = configurable.get("skill_context")

    if not tool_gateway or not skill_context:
        return {"action": "finalize", "error": "No tool gateway configured"}

    last_msg = state.messages[-1] if state.messages else {}
    decision = _extract_llm_decision(str(last_msg.get("content", "")))
    tool_calls_raw = decision.get("tool_calls", [])

    results: list[ToolResult] = []
    for tc in tool_calls_raw:
        call = ToolCall(
            tool_name=str(tc.get("name", "")),
            params=dict(tc.get("args", {}) or {}),
            task_id=skill_context.root_task_id,
        )
        ctx = ToolContext(
            user_id=skill_context.root_user_id,
            task_id=skill_context.root_task_id,
            trace_id=skill_context.trace_id,
            agent_id=skill_context.agent_id,
            checkpoint_ns=skill_context.checkpoint_ns,
            policy=configurable.get("resolved_policy"),
        )
        result = await tool_gateway.execute(call, ctx)
        results.append(result)
        obs = result.output if result.success else (result.error or "unknown error")
        state.messages.append({
            "role": "tool",
            "content": obs,
            "tool_name": call.tool_name,
        })

    summary = "; ".join(
        f"{r.output[:100]}" if r.success else f"ERROR: {r.error}"
        for r in results
    )
    return {
        "messages": state.messages,
        "observation_summary": summary[:500],
        "action": None,
    }


async def ask_user(state: AgentState) -> dict[str, Any]:
    from langgraph.config import get_config
    cfg = get_config()
    configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
    skill_context = configurable.get("skill_context")

    last_msg = state.messages[-1] if state.messages else {}
    decision = _extract_llm_decision(str(last_msg.get("content", "")))

    ask_intent = {
        "root_task_id": skill_context.root_task_id if skill_context else "",
        "agent_id": state.agent_id,
        "checkpoint_ns": skill_context.checkpoint_ns if skill_context else "",
        "nested_graph": state.agent_id,
        "graph_node": "ask_user",
        "question_id": f"q_{state.agent_id}_{state.iteration}",
        "question": decision.get("user_question", "请提供更多信息"),
        "content": decision.get("user_question", "请提供更多信息"),
        "interrupt_type": "human_input",
    }
    return {
        "action": None,
        "status": "waiting_for_user",
        "resume_metadata": ask_intent,
    }


async def finalize(state: AgentState) -> dict[str, Any]:
    return {
        "status": "done",
        "draft_result": state.draft_result or "任务已完成",
    }


async def observe(state: AgentState) -> dict[str, Any]:
    over_limit = state.iteration >= state.max_iterations
    return {
        "status": "done" if over_limit else "running",
        "error": f"达到最大迭代次数 ({state.max_iterations})" if over_limit else None,
    }


def route_after_decide(state: AgentState) -> str:
    if state.status in ("done", "failed"):
        return "finalize"
    action = state.action
    if action == "call_tool":
        return "call_tool"
    if action == "ask_user":
        return "ask_user"
    return "finalize"


def route_after_call(state: AgentState) -> str:
    if state.iteration >= state.max_iterations:
        return "finalize"
    return "reason"


def build_react_graph() -> StateGraph:
    """构建通用 ReAct Sub Graph。

    所有 agent_type 使用此函数创建，差异通过 configurable 传入的
    skill_context.skill_domain / skill_hints 控制。
    """
    graph = StateGraph(AgentState)

    graph.add_node("prepare_context", prepare_context)
    graph.add_node("reason", reason)
    graph.add_node("select_skill", select_skill)
    graph.add_node("load_guidance", load_guidance)
    graph.add_node("decide_action", decide_action)
    graph.add_node("call_tool", call_tool)
    graph.add_node("ask_user", ask_user)
    graph.add_node("observe", observe)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "prepare_context")
    graph.add_edge("prepare_context", "reason")
    graph.add_edge("reason", "select_skill")
    graph.add_edge("select_skill", "load_guidance")
    graph.add_edge("load_guidance", "decide_action")
    graph.add_conditional_edges(
        "decide_action",
        route_after_decide,
        {"call_tool": "call_tool", "ask_user": "ask_user", "finalize": "finalize"},
    )
    graph.add_edge("call_tool", "observe")
    graph.add_conditional_edges(
        "observe",
        route_after_call,
        {"reason": "reason", "finalize": "finalize"},
    )
    graph.add_edge("ask_user", "reason")
    graph.add_edge("finalize", END)

    return graph.compile(name="react_sub_graph")


__all__ = ["build_react_graph"]
