from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any
from langgraph.constants import END, START
from langgraph.config import get_config
from langgraph.graph import StateGraph

from agent.coordinator.state import (
    CoordinatorConfig,
    CoordinatorState,
    ExecutionMode,
    SkillContext,
)
from agent.platform.emit import safe_emit_progress

_log = logging.getLogger("chatdada.coordinator.agent")

_OFFICE_KEYWORDS = (
    "ppt",
    "powerpoint",
    "pptx",
    "幻灯片",
    "演示文稿",
    "slide deck",
    "deck",
    "presentation",
    "docx",
    "word",
    "xlsx",
    "excel",
    "spreadsheet",
    "workbook",
    "电子表格",
    "工作簿",
)
_OFFICE_CREATE_HINTS = ("做", "写", "生成", "创建", "制作", "起草", "create", "generate", "draft")
_OFFICE_EDIT_HINTS = ("修改", "编辑", "更新", "润色", "改", "替换", "fix", "edit", "update")
_OFFICE_INSPECT_HINTS = ("查看", "检查", "分析", "读取", "提取", "总结", "inspect", "review", "analyze", "read")
_OFFICE_TRANSFORM_HINTS = ("导出", "转换", "另存为", "转成", "export", "convert")
_OFFICE_ACTION_HINTS = _OFFICE_CREATE_HINTS + _OFFICE_EDIT_HINTS + _OFFICE_INSPECT_HINTS + _OFFICE_TRANSFORM_HINTS
_OFFICE_SPECIFICITY_HINTS = ("关于", "主题", "受众", "页", "要点", "路径", "内容", "章节", "sheet", "slide")
_OFFICE_COMPLEX_HINTS = ("研究", "调研", "专利", "零报告")
_OFFICE_CAPABILITY_PATTERNS = (
    re.compile(r"(能不能|能否|可不可以|可以|会不会|是否能|能帮我|可以帮我).*(ppt|powerpoint|docx|word|xlsx|excel|演示文稿|电子表格)", re.IGNORECASE),
    re.compile(r"(ppt|powerpoint|docx|word|xlsx|excel|演示文稿|电子表格).*(能不能|能否|可不可以|可以|会不会|是否能)", re.IGNORECASE),
)
_OFFICE_PATH_RE = re.compile(r"([A-Za-z]:\\|/|~|\.pptx\b|\.docx\b|\.xlsx\b)", re.IGNORECASE)
_OFFICE_ROUTABLE_SOURCE_RE = re.compile(r"(^[A-Za-z]:\\|^/|^~)", re.IGNORECASE)


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


def _mentions_office(goal: str) -> bool:
    lowered = str(goal or "").lower()
    return any(keyword in lowered for keyword in _OFFICE_KEYWORDS)


def _is_office_capability_inquiry(goal: str) -> bool:
    text = str(goal or "").strip()
    if not text or not _mentions_office(text):
        return False
    if any(marker in text for marker in _OFFICE_SPECIFICITY_HINTS):
        return False
    if _OFFICE_PATH_RE.search(text):
        return False
    return any(pattern.search(text) for pattern in _OFFICE_CAPABILITY_PATTERNS)


def _office_skill_input(goal: str, source_files: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": goal}
    if source_files:
        payload["source_files"] = list(source_files)
    return payload


def _normalize_office_operation_hint(skill_input: dict[str, Any]) -> str:
    operation = str(skill_input.get("operation_hint", "") or "").strip().lower()
    return operation if operation in {"create", "edit", "inspect", "transform"} else ""


def _has_explicit_office_source(goal: str, skill_input: dict[str, Any]) -> bool:
    if skill_input.get("source_files"):
        return True
    file_hint = str(skill_input.get("file_hint", "") or "").strip()
    if file_hint and _OFFICE_ROUTABLE_SOURCE_RE.search(file_hint):
        return True
    return bool(_OFFICE_ROUTABLE_SOURCE_RE.search(str(goal or "")))


def _is_edit_like_office_request(goal: str) -> bool:
    lowered = str(goal or "").lower()
    if any(marker in lowered for marker in _OFFICE_CREATE_HINTS):
        return False
    return any(marker in lowered for marker in (*_OFFICE_EDIT_HINTS, *_OFFICE_INSPECT_HINTS, *_OFFICE_TRANSFORM_HINTS))


def _office_request_needs_clarification(goal: str, skill_input: dict[str, Any]) -> bool:
    text = str(goal or "").strip()
    if not text or not _mentions_office(text):
        return False
    operation_hint = _normalize_office_operation_hint(skill_input)
    if operation_hint in {"edit", "inspect", "transform"}:
        return not _has_explicit_office_source(text, skill_input)
    if operation_hint == "create":
        return False
    if not _is_edit_like_office_request(text):
        return False
    if _has_explicit_office_source(text, skill_input):
        return False
    return True


def _is_simple_office_request(goal: str) -> bool:
    text = str(goal or "").strip()
    if not text or not _mentions_office(text):
        return False
    if _is_office_capability_inquiry(text):
        return False
    if not any(marker in text for marker in _OFFICE_ACTION_HINTS):
        return False
    if any(marker in text for marker in _OFFICE_COMPLEX_HINTS):
        return False
    return True


def _is_ppt_capability_inquiry(goal: str) -> bool:
    text = str(goal or "").strip()
    return any(keyword in text.lower() for keyword in ("ppt", "powerpoint", "演示文稿", "幻灯片")) and _is_office_capability_inquiry(text)


def _ppt_request_needs_clarification(goal: str) -> bool:
    return _office_request_needs_clarification(str(goal or ""), {})


def _is_simple_ppt_generation_request(goal: str) -> bool:
    text = str(goal or "").strip()
    return any(keyword in text.lower() for keyword in ("ppt", "powerpoint", "演示文稿", "幻灯片")) and _is_simple_office_request(text)


def _base_coordinator_result(
    *,
    trace_id: str,
    execution_mode: ExecutionMode,
    goal_understanding: str,
    skill_summary: str,
    available_skills: list[Any],
    config: CoordinatorConfig,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "execution_mode": execution_mode,
        "goal_understanding": goal_understanding,
        "skill_summary": skill_summary,
        "available_skills": available_skills,
        "config": config,
        "artifact_refs": [],
        "review": {},
        "budget": {},
        "strategy_trace": [],
        "model_hints": None,
    }


def _desktop_capability_summary(state: CoordinatorState) -> str:
    from agent.hands.langchain_tools import format_desktop_capability_summary

    return format_desktop_capability_summary(
        list(state.get("desktop_tool_descriptors") or []),
    )


async def understand_goal_node(state: CoordinatorState) -> dict[str, Any]:
    """理解用户目标，判断执行模式"""
    from agent.coordinator.skills import skill_registry

    # P1 DAG resume: task_dag already restored from interrupt state — skip LLM
    if state.get("task_dag") and state.get("execution_mode") == ExecutionMode.DAG:
        return {
            "trace_id": state.get("trace_id") or str(uuid.uuid4()),
            "config": state.get("config") or CoordinatorConfig(),
            "available_skills": skill_registry.list_skills(selectable_only=True),
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
    available_skills = skill_registry.list_skills(selectable_only=True)
    config = state.get("config") or CoordinatorConfig()
    source_files = list(state.get("source_files") or [])

    if _is_office_capability_inquiry(goal):
        safe_emit_progress("progress.step", {
            "content": f"执行模式：{ExecutionMode.DIRECT.value}",
            "node": "understand_goal",
            "trace_id": trace_id,
            "execution_mode": ExecutionMode.DIRECT.value,
        })
        return _base_coordinator_result(
            trace_id=trace_id,
            execution_mode=ExecutionMode.DIRECT,
            goal_understanding="用户在确认是否可以协助处理 Office 文档，尚未提供可执行需求",
            skill_summary=skill_summary,
            available_skills=available_skills,
            config=config,
        )

    capability_summary = _desktop_capability_summary(state)
    messages = build_understand_goal_prompt(goal, skill_summary, capability_summary, source_files)

    # 调用 LLM
    try:
        llm = get_llm("orchestrator")
        lc_messages = []
        for msg in messages:
            if msg["role"] == "system":
                lc_messages.append(SystemMessage(content=msg["content"]))
            else:
                lc_messages.append(HumanMessage(content=msg["content"]))

        response = await llm.ainvoke(lc_messages, response_format={"type": "json_object"})
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
            **_base_coordinator_result(
                trace_id=trace_id,
                execution_mode=execution_mode,
                goal_understanding=str(parsed.get("goal_understanding", goal)),
                skill_summary=skill_summary,
                available_skills=available_skills,
                config=config,
            ),
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
            **_base_coordinator_result(
                trace_id=trace_id,
                execution_mode=ExecutionMode.DIRECT,
                goal_understanding=goal,
                skill_summary=skill_summary,
                available_skills=available_skills,
                config=config,
            ),
        }


async def direct_answer_node(state: CoordinatorState) -> dict[str, Any]:
    """直接回答用户（替代旧 general_chat）"""
    from agent.coordinator.prompts import build_direct_answer_prompt
    from core.models import get_llm, response_text
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    from agent.hands import ToolContext
    from agent.hands.langchain_tools import (
        build_desktop_langchain_tools,
        format_desktop_capability_summary,
    )

    goal = state.get("original_goal", "")
    conversation_context = state.get("conversation_context") or ""
    trace_id = state.get("trace_id", "")
    request_user_id = str(state.get("request_user_id", "") or "")
    desktop_tool_descriptors = list(state.get("desktop_tool_descriptors") or [])
    capability_summary = format_desktop_capability_summary(desktop_tool_descriptors)
    graph_config = get_config()
    configurable = graph_config.get("configurable", {}) if isinstance(graph_config, dict) else {}
    tool_gateway = configurable.get("tool_gateway")

    safe_emit_progress("progress.step", {"content": "生成回答...", "node": "direct_answer", "trace_id": trace_id})

    messages = build_direct_answer_prompt(goal, conversation_context, capability_summary)

    try:
        lc_messages = []
        for msg in messages:
            if msg["role"] == "system":
                lc_messages.append(SystemMessage(content=msg["content"]))
            else:
                lc_messages.append(HumanMessage(content=msg["content"]))

        full_text = ""
        if tool_gateway is not None and desktop_tool_descriptors and request_user_id:
            tool_context = ToolContext(
                user_id=request_user_id,
                task_id=trace_id,
                trace_id=trace_id,
            )
            tools = build_desktop_langchain_tools(
                desktop_tool_descriptors,
                tool_gateway,
                tool_context,
            )
            tool_map = {tool.name: tool for tool in tools}
            llm = get_llm("orchestrator").bind_tools(tools)

            for _ in range(8):
                response = await llm.ainvoke(lc_messages)
                lc_messages.append(response)
                if isinstance(response, AIMessage) and response.tool_calls:
                    for call in response.tool_calls:
                        tool_name = str(call.get("name", "") or "")
                        tool = tool_map.get(tool_name)
                        call_args = call.get("args", {}) if isinstance(call.get("args"), dict) else {}
                        call_id = str(call.get("id", "") or uuid.uuid4())
                        if tool is None:
                            tool_output = json.dumps(
                                {"success": False, "error": f"Unknown desktop tool: {tool_name}"},
                                ensure_ascii=False,
                            )
                        else:
                            tool_output = str(await tool.ainvoke(call_args))
                        lc_messages.append(
                            ToolMessage(
                                content=tool_output,
                                tool_call_id=call_id,
                                name=tool_name,
                            )
                        )
                    continue

                full_text = response_text(response)
                break
        else:
            llm = get_llm("orchestrator")
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

    if not str(full_text or "").strip():
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
    source_files = list(state.get("source_files") or [])

    if not skill_input.get("query"):
        skill_input["query"] = goal
    if source_files and not skill_input.get("source_files"):
        skill_input["source_files"] = list(source_files)

    if selected_skill in {"do_office", "do_ppt"} and _office_request_needs_clarification(str(skill_input.get("query") or goal), skill_input):
        from agent.platform.interrupts import request_interrupt

        clarification = request_interrupt({
            "content": "我可以帮你处理这份 Office 文档，但现在缺少需要操作的源文件。请上传文件，或提供明确的 .pptx / .docx / .xlsx 路径。",
            "context": "这类请求涉及编辑、检查或转换已有文件。先给出明确文件可以避免误操作或无法定位目标文档。",
            "placeholder": "例如：请修改 /Users/name/Documents/q4_report.pptx，或直接上传要处理的文件。",
            "interrupt_type": "clarification",
        })
        clarification_text = str(clarification or "").strip()
        if clarification_text:
            skill_input["query"] = f"{skill_input['query']}\n\n补充要求：{clarification_text}"

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
