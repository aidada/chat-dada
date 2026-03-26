from __future__ import annotations

import json
import logging
from typing import Any

from langgraph.constants import END, START
from langgraph.graph import StateGraph

from capabilities.memory import ResearchMemory
from core.content_utils import normalize_markdown_report
from core.models import get_llm
from agent_runtime.interaction import ask_user

from domain_agents.research.config import ResearchConfig, get_deliverable_profile
from domain_agents.research.prompts import (
    build_aggregator_messages,
    build_intake_messages,
    build_optimizer_messages,
    build_planner_messages,
    build_synthesizer_messages,
)
from domain_agents.research.reviewers import ResearchReviewGate
from domain_agents.research.state import ResearchWorkflowState
from domain_agents.research.tools import get_research_tools
from domain_agents.research.utils import (
    aggregate_module_outputs,
    best_text,
    extract_json_payload,
    fallback_brief,
    fallback_plan,
    feedback_action,
    feedback_to_revision_targets,
    lock_module_snapshot,
    merge_brief,
    normalize_plan,
)
from domain_agents.research.worker import coordinate_modules

log = logging.getLogger("chatdada.research.workflow")


def _safe_emit(event_type: str, content: str | dict[str, Any]) -> None:
    """向流式前端发事件；脱离图运行时静默失败。"""
    try:
        from langgraph.config import get_stream_writer

        payload = dict(content) if isinstance(content, dict) else {"content": content}
        payload.setdefault("event_type", event_type)
        get_stream_writer()(payload)
    except Exception:
        pass


def _append_trace(state: ResearchWorkflowState, step: str) -> list[str]:
    """把当前执行步骤追加到 workflow_trace。"""
    trace = list(state.get("workflow_trace", []))
    trace.append(step)
    return trace


async def _invoke_llm_text(role: str, messages: list[Any]) -> str:
    """统一调用模型并抽取文本结果。"""
    llm = get_llm(role)
    response = await llm.ainvoke(messages)
    return best_text(response)


def _brief_summary(brief: dict[str, Any]) -> str:
    """把 brief 压成便于 checkpoint 展示的短摘要。"""
    return (
        f"目标：{brief.get('clarified_goal', '')}\n"
        f"产物：{brief.get('deliverable_type', '')}\n"
        f"研究模式：{brief.get('research_mode', '')}\n"
        f"偏好：{brief.get('preferred_emphasis', [])}"
    )


def _plan_summary(plan: dict[str, Any]) -> str:
    """把 planner 输出压成用户可快速扫读的计划摘要。"""
    modules = plan.get("modules", [])
    return "\n".join(
        f"- {module['module_id']}: {module['title']} (depends_on={module.get('depends_on', [])})"
        for module in modules
    )


def _revision_targets_summary(targets: list[dict[str, Any]]) -> str:
    if not targets:
        return "- （当前未生成具体修订项）"
    lines: list[str] = []
    for item in targets:
        module_id = str(item.get("module_id", "") or "unknown")
        reason = str(item.get("reason", "") or "未提供原因")
        priority = str(item.get("priority", "") or "medium")
        actions = [str(action).strip() for action in item.get("actions", []) if str(action).strip()]
        action_text = "；".join(actions[:2]) if actions else "按评审意见补强"
        lines.append(f"- {module_id} [{priority}]: {reason}；建议：{action_text}")
    return "\n".join(lines)


def _draft_preview(text: str, limit: int = 1200) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return "（当前没有可展示的草稿正文）"
    preview = stripped[:limit]
    if len(stripped) > limit:
        preview += "\n...\n（草稿过长，已截断）"
    return preview


async def intake_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """任务接收阶段：把原始 query 归一化为科研 brief。"""
    query = str(state.get("query", "") or "").strip()
    input_payload = dict(state.get("input_payload", {}) or {})
    requested_profile = str(state.get("report_profile", "") or input_payload.get("report_profile", "") or "")

    brief = fallback_brief(query, requested_profile, input_payload)
    try:
        llm_text = await _invoke_llm_text(
            "research_domain",
            build_intake_messages(query, requested_profile, input_payload),
        )
        llm_payload = extract_json_payload(llm_text) or {}
        if llm_payload:
            brief = merge_brief(brief, llm_payload, input_payload)
    except Exception:
        log.warning("Research intake LLM failed; using fallback brief", exc_info=True)

    # unresolved_questions 只允许在最前面做一次澄清，避免工作流反复打断用户。
    if brief.get("unresolved_questions"):
        question = brief["unresolved_questions"][0]
        answer = await ask_user(
            question,
            context="这条澄清会决定后续科研规划的方向与评估标准。",
            placeholder="一句话说明即可",
        )
        if answer:
            brief["clarified_goal"] = f"{brief.get('clarified_goal', query)}\n用户补充：{answer}"
            brief["user_constraints"] = [*brief.get("user_constraints", []), str(answer).strip()]
            brief["unresolved_questions"] = []

    _safe_emit("step", "Research intake completed")
    _safe_emit("brief", {"status": "generated", "brief": brief})
    return {
        "brief": brief,
        "needs_replan": False,
        "needs_clarification": False,
        "workflow_trace": _append_trace(state, "intake"),
    }


async def planner_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """规划阶段：生成模块计划，而不是直接写正文。"""
    brief = dict(state.get("brief", {}) or {})
    plan = fallback_plan(brief)
    try:
        llm_text = await _invoke_llm_text(
            "research_domain",
            build_planner_messages(brief),
        )
        llm_payload = extract_json_payload(llm_text)
        if llm_payload:
            plan = normalize_plan(llm_payload, brief)
    except Exception:
        log.warning("Research planner LLM failed; using fallback plan", exc_info=True)

    module_status = {module["module_id"]: "pending" for module in plan.get("modules", [])}
    _safe_emit("step", f"Planner generated {len(plan.get('modules', []))} modules")
    _safe_emit("plan", {"status": "generated", "modules": plan.get("modules", [])})

    return {
        "plan": plan,
        "module_status": module_status,
        "module_outputs": {},
        "evidence_bank": [],
        "citation_bank": [],
        "aggregated_draft": "",
        "draft_history": [],
        "evaluations": [],
        "revision_targets": [],
        "locked_modules": {},
        "needs_replan": False,
        "revision_round": 0,
        "workflow_trace": _append_trace(state, "planner"),
    }


async def checkpoint_a_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """检查点 A：计划确认点。

    用户如果在这里改方向，直接回规划阶段重规划。
    """
    plan = state.get("plan", {})
    answer = await ask_user(
        "研究计划已生成。如果范围、产物类型或重点需要调整，请一句话说明；如无修改可忽略。",
        context=_plan_summary(plan),
        placeholder="例如：更偏实验设计，少写综述",
    )

    feedback_history = list(state.get("feedback_history", []))
    if answer:
        feedback_history.append({"checkpoint": "checkpoint_a", "content": answer})

    action = feedback_action(answer)
    brief = dict(state.get("brief", {}) or {})
    if action == "replan" or action == "revise":
        brief["user_constraints"] = [*brief.get("user_constraints", []), str(answer).strip()]

    _safe_emit("checkpoint", {"status": "visited", "checkpoint": "checkpoint_a"})
    return {
        "brief": brief,
        "feedback_history": feedback_history,
        "active_checkpoint": "checkpoint_a",
        "needs_replan": action in {"replan", "revise"},
        "workflow_trace": _append_trace(state, "checkpoint_a"),
    }


async def dispatch_modules_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """执行当前可运行的模块 wave。"""
    task_id = str(state.get("task_id", "") or "")
    memory = ResearchMemory(task_id) if task_id else None
    if memory is not None and memory.load_meta() is None:
        memory.init(str(state.get("query", "") or ""), str(state.get("report_profile", "") or ""))

    result = await coordinate_modules(
        plan=dict(state.get("plan", {}) or {}),
        brief=dict(state.get("brief", {}) or {}),
        module_outputs=dict(state.get("module_outputs", {}) or {}),
        module_status=dict(state.get("module_status", {}) or {}),
        revision_targets=list(state.get("revision_targets", []) or []),
        tools=get_research_tools(),
        memory=memory,
        config=ResearchConfig(),
    )

    total = max(len(result.get("module_status", {})), 1)
    finished = sum(
        1
        for value in result.get("module_status", {}).values()
        if value in {"completed", "locked"}
    )
    progress = finished / total
    _safe_emit("step", f"Executed research modules ({finished}/{total})")
    return {
        "module_outputs": result["module_outputs"],
        "module_status": result["module_status"],
        "evidence_bank": result["evidence_bank"],
        "citation_bank": result["citation_bank"],
        "progress": progress,
        "workflow_trace": _append_trace(state, "dispatch_modules"),
    }


async def aggregate_draft_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """把模块草案聚合为可评估的中间稿。"""
    brief = dict(state.get("brief", {}) or {})
    module_outputs = dict(state.get("module_outputs", {}) or {})
    fallback = aggregate_module_outputs(brief, module_outputs)
    draft = fallback
    try:
        text = await _invoke_llm_text(
            "research_domain",
            build_aggregator_messages(brief, module_outputs),
        )
        if text.strip():
            draft = normalize_markdown_report(text)
    except Exception:
        log.warning("Draft aggregation LLM failed; using fallback draft", exc_info=True)

    task_id = str(state.get("task_id", "") or "")
    if task_id:
        memory = ResearchMemory(task_id)
        if memory.load_meta() is None:
            memory.init(str(state.get("query", "") or ""), str(state.get("report_profile", "") or ""))
        memory.save_summary(len(state.get("draft_history", [])) + 1, draft)

    draft_history = list(state.get("draft_history", []))
    # draft_history 保留每一版中间稿，便于定位“哪一轮修订把内容写坏了”。
    draft_history.append({"draft": draft, "at": len(draft_history) + 1})
    _safe_emit("step", "Aggregated research draft")
    return {
        "aggregated_draft": draft,
        "draft_history": draft_history,
        "workflow_trace": _append_trace(state, "aggregate_draft"),
    }


async def evaluate_draft_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """对中间稿做结构化评估，并产出 revision_targets。"""
    review = await ResearchReviewGate().evaluate(
        {
            "brief": state.get("brief", {}),
            "plan": state.get("plan", {}),
            "report": state.get("aggregated_draft", ""),
            "module_outputs": state.get("module_outputs", {}),
            "evidence_bank": state.get("evidence_bank", []),
            "citation_bank": state.get("citation_bank", []),
        }
    )
    evaluation = {
        "passed": review.passed,
        "needs_replan": review.needs_replan,
        "summary": review.summary,
        "dimensions": [
            {
                "name": item.name,
                "score": item.score,
                "passed": item.passed,
                "strengths": item.strengths,
                "weaknesses": item.weaknesses,
                "affected_modules": item.affected_modules,
                "metadata": item.metadata,
            }
            for item in review.dimensions
        ],
        "revision_targets": [
            {
                "module_id": item.module_id,
                "reason": item.reason,
                "priority": item.priority,
                "actions": item.actions,
                "preserve_constraints": item.preserve_constraints,
                "requires_new_evidence": item.requires_new_evidence,
                "metadata": item.metadata,
            }
            for item in review.revision_targets
        ],
        "lock_modules": list(review.lock_modules),
        "user_feedback_required": review.user_feedback_required,
        "issues": [
            {
                "severity": item.severity,
                "message": item.message,
                "metadata": item.metadata,
            }
            for item in review.issues
        ],
    }

    evaluations = list(state.get("evaluations", []))
    evaluations.append(evaluation)

    module_status = dict(state.get("module_status", {}) or {})
    module_outputs = dict(state.get("module_outputs", {}) or {})
    targeted_ids = {item["module_id"] for item in evaluation["revision_targets"]}
    # 评估通过的模块直接锁定；命中的低分模块转为 needs_revision，
    # 后续优化阶段只能动这些模块。
    for module_id in evaluation["lock_modules"]:
        if module_id in module_outputs:
            module_outputs[module_id]["locked"] = True
            module_status[module_id] = "locked"
    for module_id in targeted_ids:
        if module_id in module_outputs:
            module_outputs[module_id]["locked"] = False
        module_status[module_id] = "needs_revision"

    progress = sum(1 for value in module_status.values() if value in {"completed", "locked"}) / max(len(module_status), 1)
    _safe_emit(
        "review",
        {
            "status": "passed" if review.passed else "failed",
            "summary": review.summary,
            "issues": evaluation["issues"],
        },
    )
    return {
        "evaluations": evaluations,
        "revision_targets": evaluation["revision_targets"],
        "locked_modules": {
            **dict(state.get("locked_modules", {}) or {}),
            **lock_module_snapshot(module_outputs, evaluation["lock_modules"]),
        },
        "module_outputs": module_outputs,
        "module_status": module_status,
        "needs_replan": review.needs_replan,
        "progress": progress,
        "workflow_trace": _append_trace(state, "evaluate_draft"),
    }


async def checkpoint_b_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """检查点 B：中间稿评审后的人工纠偏点。"""
    latest = (state.get("evaluations") or [{}])[-1]
    revision_targets = list(state.get("revision_targets", []) or [])
    question = (
        "当前草案评审未通过。\n\n"
        f"评审摘要：{latest.get('summary', '未提供')}\n\n"
        "待修订模块：\n"
        f"{_revision_targets_summary(revision_targets)}\n\n"
        "当前草稿摘录：\n"
        f"{_draft_preview(str(state.get('aggregated_draft', '') or ''))}\n\n"
        "如果需要改方向请直接说明；否则回复“继续修订”或“继续”。"
    )
    answer = await ask_user(
        question,
        context=json.dumps(
            {
                "summary": latest.get("summary", ""),
                "revision_targets": latest.get("revision_targets", []),
                "aggregated_draft": str(state.get("aggregated_draft", "") or ""),
            },
            ensure_ascii=False,
            indent=2,
        ),
        placeholder="例如：更偏实验方案，减少引言综述",
    )

    feedback_history = list(state.get("feedback_history", []))
    if answer:
        feedback_history.append({"checkpoint": "checkpoint_b", "content": answer})

    action = feedback_action(answer)
    if action == "revise":
        revision_targets.extend(feedback_to_revision_targets(str(answer), dict(state.get("plan", {}) or {})))

    _safe_emit("checkpoint", {"status": "visited", "checkpoint": "checkpoint_b"})
    return {
        "feedback_history": feedback_history,
        # evaluator 已经判定方向不对时，不能被“继续修订”无意清零；
        # 否则系统会沿着错误计划反复做局部修补。
        "needs_replan": bool(state.get("needs_replan")) or action == "replan",
        "revision_targets": revision_targets,
        "active_checkpoint": "checkpoint_b",
        "workflow_trace": _append_trace(state, "checkpoint_b"),
    }


async def optimize_modules_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """只修订 revision_targets 命中的模块。"""
    revision_targets = list(state.get("revision_targets", []) or [])
    if not revision_targets:
        return {"workflow_trace": _append_trace(state, "optimize_modules")}

    optimizer_context = ""
    try:
        optimizer_context = await _invoke_llm_text(
            "research_domain",
            build_optimizer_messages(
                dict(state.get("brief", {}) or {}),
                revision_targets,
                dict(state.get("locked_modules", {}) or {}),
                dict(state.get("module_outputs", {}) or {}),
                list(state.get("feedback_history", []) or []),
            ),
        )
    except Exception:
        log.warning("Optimizer context LLM failed; using direct revision targets", exc_info=True)

    module_status = dict(state.get("module_status", {}) or {})
    # 先显式标记需要返工的模块，再交给 coordinate_modules 做定向修订。
    for target in revision_targets:
        module_status[target["module_id"]] = "needs_revision"

    task_id = str(state.get("task_id", "") or "")
    memory = ResearchMemory(task_id) if task_id else None
    result = await coordinate_modules(
        plan=dict(state.get("plan", {}) or {}),
        brief=dict(state.get("brief", {}) or {}),
        module_outputs=dict(state.get("module_outputs", {}) or {}),
        module_status=module_status,
        revision_targets=revision_targets,
        tools=get_research_tools(),
        memory=memory,
        config=ResearchConfig(),
        optimizer_context=optimizer_context,
    )

    _safe_emit("step", f"Optimized {len(revision_targets)} research modules")
    return {
        "module_outputs": result["module_outputs"],
        "module_status": result["module_status"],
        "evidence_bank": result["evidence_bank"],
        "citation_bank": result["citation_bank"],
        "revision_round": int(state.get("revision_round", 0)) + 1,
        "workflow_trace": _append_trace(state, "optimize_modules"),
    }


async def checkpoint_c_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """检查点 C：最终整合前的最后一次人工确认。"""
    brief = dict(state.get("brief", {}) or {})
    profile = get_deliverable_profile(brief.get("deliverable_type"))
    answer = await ask_user(
        "模块评审已通过。若还要继续微调，请说明；如无修改可忽略，系统将输出最终稿。",
        context=(
            f"目标产物：{profile.label}\n"
            f"目标章节：{list(profile.final_sections)}\n\n"
            f"{_brief_summary(brief)}"
        ),
        placeholder="例如：把实验建议写得更具体一些",
    )

    feedback_history = list(state.get("feedback_history", []))
    if answer:
        feedback_history.append({"checkpoint": "checkpoint_c", "content": answer})

    action = feedback_action(answer)
    if action == "revise":
        revision_targets = feedback_to_revision_targets(str(answer), dict(state.get("plan", {}) or {}))
    else:
        revision_targets = []

    _safe_emit("checkpoint", {"status": "visited", "checkpoint": "checkpoint_c"})
    return {
        "feedback_history": feedback_history,
        "needs_replan": action == "replan",
        "revision_targets": revision_targets,
        "active_checkpoint": "checkpoint_c",
        "workflow_trace": _append_trace(state, "checkpoint_c"),
    }


async def synthesize_final_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """在所有关键模块通过评估后，整合为最终科研输出。"""
    brief = dict(state.get("brief", {}) or {})
    module_outputs = dict(state.get("module_outputs", {}) or {})
    latest_evaluation = (state.get("evaluations") or [{}])[-1]
    final_text = normalize_markdown_report(str(state.get("aggregated_draft", "") or ""))
    try:
        text = await _invoke_llm_text(
            "research_domain",
            build_synthesizer_messages(brief, module_outputs, latest_evaluation),
        )
        if text.strip():
            final_text = normalize_markdown_report(text)
    except Exception:
        log.warning("Synthesizer LLM failed; using aggregated draft", exc_info=True)

    _safe_emit("step", "Synthesized final research output")
    return {
        "final_result": final_text,
        "workflow_trace": _append_trace(state, "synthesize_final"),
    }


def build_research_workflow_graph(config: ResearchConfig | None = None) -> Any:
    """构建科研工作流主图。"""
    cfg = config or ResearchConfig()
    graph = StateGraph(ResearchWorkflowState)

    # 节点职责保持单一：接收/规划/执行/聚合/评估/修订/终稿。
    graph.add_node("intake", intake_node)
    graph.add_node("planner", planner_node)
    graph.add_node("checkpoint_a", checkpoint_a_node)
    graph.add_node("dispatch_modules", dispatch_modules_node)
    graph.add_node("aggregate_draft", aggregate_draft_node)
    graph.add_node("evaluate_draft", evaluate_draft_node)
    graph.add_node("checkpoint_b", checkpoint_b_node)
    graph.add_node("optimize_modules", optimize_modules_node)
    graph.add_node("checkpoint_c", checkpoint_c_node)
    graph.add_node("synthesize_final", synthesize_final_node)

    def _route_after_checkpoint_a(state: ResearchWorkflowState) -> str:
        return "planner" if state.get("needs_replan") else "dispatch_modules"

    def _route_after_evaluate(state: ResearchWorkflowState) -> str:
        evaluations = state.get("evaluations") or []
        if evaluations and evaluations[-1].get("passed"):
            return "checkpoint_c"
        return "checkpoint_b"

    def _route_after_checkpoint_b(state: ResearchWorkflowState) -> str:
        if state.get("needs_replan"):
            return "planner"
        # 到达最大修订轮数后不再无限循环，直接尝试收束到最终稿。
        if int(state.get("revision_round", 0) or 0) >= cfg.max_revision_cycles:
            return "synthesize_final"
        return "optimize_modules"

    def _route_after_checkpoint_c(state: ResearchWorkflowState) -> str:
        if state.get("needs_replan"):
            return "planner"
        if state.get("revision_targets"):
            return "optimize_modules"
        return "synthesize_final"

    # 主流程：
    # 接收 -> 规划 -> 检查点 A -> 执行模块 -> 聚合草案 -> 评估
    # 评估失败时经过检查点 B / optimize_modules 进入下一轮
    # 评估通过时经过检查点 C 收束到最终稿
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "planner")
    graph.add_edge("planner", "checkpoint_a")
    graph.add_conditional_edges(
        "checkpoint_a",
        _route_after_checkpoint_a,
        {
            "planner": "planner",
            "dispatch_modules": "dispatch_modules",
        },
    )
    graph.add_edge("dispatch_modules", "aggregate_draft")
    graph.add_edge("aggregate_draft", "evaluate_draft")
    graph.add_conditional_edges(
        "evaluate_draft",
        _route_after_evaluate,
        {
            "checkpoint_b": "checkpoint_b",
            "checkpoint_c": "checkpoint_c",
        },
    )
    graph.add_conditional_edges(
        "checkpoint_b",
        _route_after_checkpoint_b,
        {
            "planner": "planner",
            "optimize_modules": "optimize_modules",
            "synthesize_final": "synthesize_final",
        },
    )
    graph.add_edge("optimize_modules", "aggregate_draft")
    graph.add_conditional_edges(
        "checkpoint_c",
        _route_after_checkpoint_c,
        {
            "planner": "planner",
            "optimize_modules": "optimize_modules",
            "synthesize_final": "synthesize_final",
        },
    )
    graph.add_edge("synthesize_final", END)

    return graph.compile(name="research_workflow")
