from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import RetryPolicy

from agent.capabilities.memory import ResearchMemory
from core.content_utils import normalize_markdown_report
from core.models import get_llm
from agent.runtime.interaction import ask_user

from agent.workflows.research.config import ResearchConfig, get_deliverable_profile
from agent.workflows.research.prompts import (
    build_aggregator_messages,
    build_intake_messages,
    build_optimizer_messages,
    build_planner_messages,
    build_synthesizer_messages,
)
from agent.workflows.research.reviewers import ResearchReviewGate
from agent.workflows.research.state import ResearchWorkflowState
from agent.workflows.research.tools import get_research_tools
from agent.workflows.research.utils import (
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
from agent.workflows.research.worker import coordinate_modules
from agent.platform.emit import safe_emit_progress_with_content as _safe_emit

log = logging.getLogger("chatdada.research.workflow")

WORKFLOW_LLM_NODE_MAX_ATTEMPTS = 3
CHECKPOINT_C_PROMPT = "模块评审已通过。若还要继续微调，请说明；如无修改可忽略，系统将输出最终稿。"


def _task_download_url(task_id: str, relative_path: str) -> str:
    encoded = quote(relative_path, safe="")
    return f"/tasks/{task_id}/artifact-file?path={encoded}"


def _stage_file_ref(task_id: str, relative_path: str, *, name: str, file_type: str = "file") -> dict[str, Any]:
    return {
        "type": file_type,
        "name": name,
        "path": relative_path,
        "url": _task_download_url(task_id, relative_path),
    }


def _emit_stage_artifacts(
    task_id: str,
    *,
    stage_id: str,
    stage_title: str,
    files: list[dict[str, Any]],
    status: str = "ready",
) -> None:
    if not task_id:
        return
    _safe_emit(
        "stage_artifacts",
        {
            "content": stage_title,
            "stage_id": stage_id,
            "stage_title": stage_title,
            "files": files,
            "status": status,
        },
    )


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


def _should_retry_workflow_llm_node(exc: Exception) -> bool:
    transient_httpx_errors = (
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.ProtocolError,
    )
    if isinstance(exc, transient_httpx_errors):
        return True

    name = exc.__class__.__name__
    module = exc.__class__.__module__
    if name in {"APIConnectionError", "APITimeoutError", "InternalServerError"}:
        return module.startswith("openai")
    if name == "ServerError" and module.startswith("google.genai"):
        return True

    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
            "remoteprotocolerror",
            "incomplete chunked read",
            "peer closed connection",
            "host error",
        )
    )


def _workflow_llm_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        max_attempts=WORKFLOW_LLM_NODE_MAX_ATTEMPTS,
        retry_on=_should_retry_workflow_llm_node,
    )


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


def _plan_markdown(plan: dict[str, Any]) -> str:
    modules = list(plan.get("modules", []) or [])
    if not modules:
        return "# 研究计划\n\n当前未生成可展示的研究计划。"

    lines = ["# 研究计划", ""]
    for index, module in enumerate(modules, start=1):
        module_id = str(module.get("module_id", "") or f"module_{index}")
        title = str(module.get("title", "") or module_id)
        objective = str(module.get("objective", "") or "待补充模块目标")
        depends_on = [str(item) for item in module.get("depends_on", []) or [] if str(item).strip()]
        lines.append(f"## {index}. {title}")
        lines.append("")
        lines.append(f"- 模块 ID：`{module_id}`")
        lines.append(f"- 目标：{objective}")
        if depends_on:
            lines.append(f"- 依赖：{', '.join(depends_on)}")
        owner_role = str(module.get("owner_role", "") or "").strip()
        if owner_role:
            lines.append(f"- 执行角色：{owner_role}")
        lines.append("")
    return "\n".join(lines).strip()


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


def _blocked_modules_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "- （当前无阻塞模块）"
    return "\n".join(
        f"- {item.get('module_id', 'unknown')}: {item.get('reason', '未提供 blocker')}"
        for item in items
    )


def _active_modules(module_status: dict[str, str]) -> list[str]:
    return [
        module_id
        for module_id, status in module_status.items()
        if status in {"pending", "running", "needs_revision"}
    ]


def _actionable_revision_targets(
    revision_targets: list[dict[str, Any]],
    module_status: dict[str, str],
) -> list[dict[str, Any]]:
    actionable: list[dict[str, Any]] = []
    for target in revision_targets:
        module_id = str(target.get("module_id", "") or "").strip()
        if not module_id:
            continue
        status = str(module_status.get(module_id, "") or "").strip()
        if status in {"completed", "locked"}:
            continue
        actionable.append(target)
    return actionable


def _budget_feedback_action(answer: str | None) -> str:
    lowered = str(answer or "").strip().lower()
    if not lowered:
        return ""
    if any(token in lowered for token in ("停止", "先这样", "按当前", "交付", "accept", "current result", "不用继续")):
        return "accept"
    if any(token in lowered for token in ("继续", "扩", "加预算", "补检索", "继续搜索", "继续修订", "retry", "more evidence")):
        return "extend"
    return ""


def _apply_evaluation_budget_bonus(
    budget: dict[str, Any],
    last_evaluation_diff: dict[str, Any],
    cfg: ResearchConfig,
) -> dict[str, Any]:
    if not budget or not last_evaluation_diff:
        return budget
    module_budgets = dict(budget.get("module_budgets", {}) or {})
    changes = {
        str(item.get("name", "") or ""): float(item.get("delta", 0.0) or 0.0)
        for item in last_evaluation_diff.get("dimension_changes", []) or []
    }

    def _bump(module_id: str) -> None:
        entry = dict(module_budgets.get(module_id, {}) or {})
        if not entry:
            return
        soft_budget = int(entry.get("soft_budget", 0) or 0)
        hard_budget = int(entry.get("hard_budget", 0) or 0)
        if soft_budget >= hard_budget:
            return
        entry["soft_budget"] = min(hard_budget, soft_budget + cfg.dynamic_budget_score_bonus)
        module_budgets[module_id] = entry

    if changes.get("citation_relevance_coverage", 0.0) >= 0.08:
        _bump("related_work")
    if changes.get("argument_chain_completeness", 0.0) >= 0.08:
        for module_id in ("argument_map", "limitations", "contributions"):
            _bump(module_id)
    if changes.get("experimental_feasibility", 0.0) >= 0.08:
        _bump("experiment_design")
    if changes.get("methodological_rigor", 0.0) >= 0.08:
        _bump("method_candidates")

    budget["module_budgets"] = module_budgets
    return budget


def _extend_budget_after_user_feedback(
    budget: dict[str, Any],
    module_status: dict[str, str],
    cfg: ResearchConfig,
) -> dict[str, Any]:
    module_budgets = dict(budget.get("module_budgets", {}) or {})
    for module_id, entry in module_budgets.items():
        item = dict(entry or {})
        if not bool(item.get("terminal_blocked")):
            continue
        item["hard_budget"] = int(item.get("hard_budget", 0) or 0) + cfg.dynamic_budget_hard_extension
        item["soft_budget"] = max(int(item.get("soft_budget", 0) or 0), int(item.get("consumed_rounds", 0) or 0) + 1)
        item["stall_count"] = 0
        item["terminal_blocked"] = False
        item["pending_instruction"] = (
            "用户已同意继续消耗预算。下一轮必须改写检索策略，优先补齐未覆盖的关键文献方向，避免重复来源。"
        )
        module_budgets[module_id] = item
    budget.update(
        {
            "module_budgets": module_budgets,
            "awaiting_user_decision": False,
            "last_user_decision": "extend",
            "status": "active",
            "active_modules": [
                module_id
                for module_id, status in module_status.items()
                if status in {"pending", "running", "needs_revision", "blocked", "skipped"}
            ],
        }
    )
    return budget


def _build_evaluation_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    previous = previous or {}
    previous_dims = {
        str(item.get("name", "") or ""): float(item.get("score", 0.0) or 0.0)
        for item in previous.get("dimensions", []) or []
        if str(item.get("name", "") or "").strip()
    }
    current_dims = {
        str(item.get("name", "") or ""): float(item.get("score", 0.0) or 0.0)
        for item in current.get("dimensions", []) or []
        if str(item.get("name", "") or "").strip()
    }
    dimension_changes = []
    for name in sorted(set(previous_dims) | set(current_dims)):
        before = previous_dims.get(name, 0.0)
        after = current_dims.get(name, 0.0)
        dimension_changes.append(
            {
                "name": name,
                "previous_score": before,
                "current_score": after,
                "delta": round(after - before, 3),
            }
        )

    previous_modules = {str(item.get("module_id", "") or "") for item in previous.get("revision_targets", []) or []}
    current_modules = {str(item.get("module_id", "") or "") for item in current.get("revision_targets", []) or []}
    return {
        "dimension_changes": dimension_changes,
        "changed_modules": sorted(module_id for module_id in current_modules - previous_modules if module_id),
        "unchanged_modules": sorted(module_id for module_id in current_modules & previous_modules if module_id),
        "resolved_modules": sorted(module_id for module_id in previous_modules - current_modules if module_id),
    }


def _draft_preview(text: str, limit: int = 1200) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return "（当前没有可展示的草稿正文）"
    preview = stripped[:limit]
    if len(stripped) > limit:
        preview += "\n...\n（草稿过长，已截断）"
    return preview


def _is_review_driven_replan(state: ResearchWorkflowState) -> bool:
    return (
        bool(state.get("needs_replan"))
        and str(state.get("active_checkpoint", "") or "") == "checkpoint_b"
        and bool(state.get("evaluations"))
    )


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
    review_driven_replan = _is_review_driven_replan(state)
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
        "aggregated_draft": str(state.get("aggregated_draft", "") or "") if review_driven_replan else "",
        "draft_history": list(state.get("draft_history", []) or []) if review_driven_replan else [],
        "evaluations": list(state.get("evaluations", []) or []) if review_driven_replan else [],
        "revision_targets": [],
        "locked_modules": {},
        "blocked_modules": [],
        "active_modules": list(module_status),
        "last_evaluation_diff": (
            dict(state.get("last_evaluation_diff", {}) or {}) if review_driven_replan else {}
        ),
        "budget": dict(state.get("budget", {}) or {}) if review_driven_replan else {},
        "needs_replan": False,
        "skip_checkpoint_a_once": review_driven_replan,
        "revision_round": int(state.get("revision_round", 0) or 0) if review_driven_replan else 0,
        "workflow_trace": _append_trace(state, "planner"),
    }


async def checkpoint_a_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """检查点 A：计划确认点。

    用户如果在这里改方向，直接回规划阶段重规划。
    """
    plan = state.get("plan", {})
    task_id = str(state.get("task_id", "") or "")
    plan_files: list[dict[str, Any]] = []
    if task_id and plan:
        memory = ResearchMemory(task_id)
        if memory.load_meta() is None:
            memory.init(str(state.get("query", "") or ""), str(state.get("report_profile", "") or ""))
        plan_path = memory.task_dir / "research_plan.md"
        plan_path.write_text(_plan_markdown(plan), encoding="utf-8")
        plan_files.append(
            _stage_file_ref(
                task_id,
                "research_plan.md",
                name="研究计划.md",
                file_type="markdown",
            )
        )
    _emit_stage_artifacts(
        task_id,
        stage_id="checkpoint_a",
        stage_title="研究计划确认",
        files=plan_files,
    )
    if state.get("skip_checkpoint_a_once"):
        _emit_stage_artifacts(
            task_id,
            stage_id="checkpoint_a",
            stage_title="研究计划确认",
            files=[],
            status="cleared",
        )
        _safe_emit("checkpoint", {"status": "visited", "checkpoint": "checkpoint_a"})
        return {
            "active_checkpoint": "checkpoint_a",
            "needs_replan": False,
            "skip_checkpoint_a_once": False,
            "workflow_trace": _append_trace(state, "checkpoint_a"),
        }
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

    _emit_stage_artifacts(
        task_id,
        stage_id="checkpoint_a",
        stage_title="研究计划确认",
        files=[],
        status="cleared",
    )
    _safe_emit("checkpoint", {"status": "visited", "checkpoint": "checkpoint_a"})
    return {
        "brief": brief,
        "feedback_history": feedback_history,
        "active_checkpoint": "checkpoint_a",
        "needs_replan": action in {"replan", "revise"},
        "skip_checkpoint_a_once": False,
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
        budget=dict(state.get("budget", {}) or {}),
        existing_evidence_bank=list(state.get("evidence_bank", []) or []),
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
    blocked_modules = list(result.get("blocked_modules", []) or [])
    active_modules = _active_modules(dict(result.get("module_status", {}) or {}))
    _safe_emit(
        "step",
        {
            "status": "modules_executed",
            "content": f"Executed research modules ({finished}/{total})",
            "blocked_modules": blocked_modules,
            "active_modules": active_modules,
        },
    )
    return {
        "module_outputs": result["module_outputs"],
        "module_status": result["module_status"],
        "evidence_bank": result["evidence_bank"],
        "citation_bank": result["citation_bank"],
        "blocked_modules": blocked_modules,
        "active_modules": active_modules,
        "budget": dict(result.get("budget", {}) or {}),
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
        (memory.task_dir / "aggregated_draft.md").write_text(draft, encoding="utf-8")
        memory.save_checkpoint(
            len(state.get("draft_history", [])) * 2 + 1,
            {
                "aggregated_draft": draft,
                "module_outputs": module_outputs,
                "module_status": dict(state.get("module_status", {}) or {}),
                "blocked_modules": list(state.get("blocked_modules", []) or []),
                "revision_round": int(state.get("revision_round", 0) or 0),
            },
        )

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
    previous_evaluation = evaluations[-1] if evaluations else None
    evaluations.append(evaluation)
    last_evaluation_diff = _build_evaluation_diff(previous_evaluation, evaluation)
    budget = _apply_evaluation_budget_bonus(
        dict(state.get("budget", {}) or {}),
        last_evaluation_diff,
        ResearchConfig(),
    )

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
        if str(module_status.get(module_id, "") or "") in {"blocked", "skipped"}:
            continue
        if module_id in module_outputs:
            module_outputs[module_id]["locked"] = False
        module_status[module_id] = "needs_revision"

    progress = sum(1 for value in module_status.values() if value in {"completed", "locked"}) / max(len(module_status), 1)
    blocked_modules = list(state.get("blocked_modules", []) or [])
    active_modules = _active_modules(module_status)

    task_id = str(state.get("task_id", "") or "")
    if task_id:
        memory = ResearchMemory(task_id)
        if memory.load_meta() is None:
            memory.init(str(state.get("query", "") or ""), str(state.get("report_profile", "") or ""))
        memory.save_checkpoint(
            len(evaluations) * 2,
            {
                "evaluation": evaluation,
                "last_evaluation_diff": last_evaluation_diff,
                "module_outputs": module_outputs,
                "module_status": module_status,
                "blocked_modules": blocked_modules,
                "revision_round": int(state.get("revision_round", 0) or 0),
                "budget": budget,
            },
        )

    _safe_emit(
        "review",
        {
            "status": "passed" if review.passed else "failed",
            "summary": review.summary,
            "issues": evaluation["issues"],
            "blocked_modules": blocked_modules,
            "last_evaluation_diff": last_evaluation_diff,
            "budget": budget,
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
        "blocked_modules": blocked_modules,
        "active_modules": active_modules,
        "last_evaluation_diff": last_evaluation_diff,
        "budget": budget,
        "needs_replan": review.needs_replan,
        "progress": progress,
        "workflow_trace": _append_trace(state, "evaluate_draft"),
    }


async def checkpoint_b_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """检查点 B：中间稿评审后的人工纠偏点。"""
    latest = (state.get("evaluations") or [{}])[-1]
    revision_targets = list(state.get("revision_targets", []) or [])
    budget = dict(state.get("budget", {}) or {})
    budget_prompt = ""
    if budget.get("awaiting_user_decision"):
        budget_prompt = (
            "当前有模块已经达到 hard budget 上限。\n"
            "如果你希望继续完成任务，请直接回复“继续”或说明继续补检索；"
            "如果接受当前部分结果，请回复“按当前结果交付”。\n\n"
        )
    task_id = str(state.get("task_id", "") or "")
    draft_files: list[dict[str, Any]] = []
    if task_id and str(state.get("aggregated_draft", "") or "").strip():
        draft_files.append(
            _stage_file_ref(
                task_id,
                "aggregated_draft.md",
                name="当前研究草稿.md",
                file_type="markdown",
            )
        )
    _emit_stage_artifacts(
        task_id,
        stage_id="checkpoint_b",
        stage_title="草稿评审待修订",
        files=draft_files,
    )
    question = (
        "当前草案评审未通过。\n\n"
        f"评审摘要：{latest.get('summary', '未提供')}\n\n"
        f"{budget_prompt}"
        "待修订模块：\n"
        f"{_revision_targets_summary(revision_targets)}\n\n"
        "当前 blocker：\n"
        f"{_blocked_modules_summary(list(state.get('blocked_modules', []) or []))}\n\n"
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
                "blocked_modules": list(state.get("blocked_modules", []) or []),
                "aggregated_draft": str(state.get("aggregated_draft", "") or ""),
                "budget": budget,
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
    budget_feedback = _budget_feedback_action(answer)
    if budget.get("awaiting_user_decision") and budget_feedback == "extend":
        budget = _extend_budget_after_user_feedback(
            budget,
            dict(state.get("module_status", {}) or {}),
            ResearchConfig(),
        )
    elif budget.get("awaiting_user_decision") and budget_feedback == "accept":
        budget["last_user_decision"] = "accept"
    if action == "revise":
        revision_targets.extend(feedback_to_revision_targets(str(answer), dict(state.get("plan", {}) or {})))

    _emit_stage_artifacts(
        task_id,
        stage_id="checkpoint_b",
        stage_title="草稿评审待修订",
        files=[],
        status="cleared",
    )
    _safe_emit("checkpoint", {"status": "visited", "checkpoint": "checkpoint_b"})
    return {
        "feedback_history": feedback_history,
        # evaluator 已经判定方向不对时，不能被“继续修订”无意清零；
        # 否则系统会沿着错误计划反复做局部修补。
        "needs_replan": bool(state.get("needs_replan")) or action == "replan",
        "revision_targets": revision_targets,
        "budget": budget,
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
        budget=dict(state.get("budget", {}) or {}),
        existing_evidence_bank=list(state.get("evidence_bank", []) or []),
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
        "blocked_modules": list(result.get("blocked_modules", []) or []),
        "active_modules": _active_modules(dict(result.get("module_status", {}) or {})),
        "budget": dict(result.get("budget", {}) or {}),
        "revision_round": int(state.get("revision_round", 0)) + 1,
        "workflow_trace": _append_trace(state, "optimize_modules"),
    }


async def checkpoint_c_node(state: ResearchWorkflowState) -> dict[str, Any]:
    """检查点 C：最终整合前的最后一次人工确认。"""
    brief = dict(state.get("brief", {}) or {})
    profile = get_deliverable_profile(brief.get("deliverable_type"))
    task_id = str(state.get("task_id", "") or "")
    final_stage_files: list[dict[str, Any]] = []
    if task_id and str(state.get("aggregated_draft", "") or "").strip():
        final_stage_files.append(
            _stage_file_ref(
                task_id,
                "aggregated_draft.md",
                name="成稿前确认稿.md",
                file_type="markdown",
            )
        )
    _emit_stage_artifacts(
        task_id,
        stage_id="checkpoint_c",
        stage_title="最终成稿前确认",
        files=final_stage_files,
    )
    answer = await ask_user(
        CHECKPOINT_C_PROMPT,
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

    _emit_stage_artifacts(
        task_id,
        stage_id="checkpoint_c",
        stage_title="最终成稿前确认",
        files=[],
        status="cleared",
    )
    _safe_emit("checkpoint", {"status": "visited", "checkpoint": "checkpoint_c"})
    return {
        "feedback_history": feedback_history,
        "needs_replan": action == "replan",
        "revision_targets": revision_targets,
        "active_checkpoint": "checkpoint_c",
        "workflow_trace": _append_trace(state, "checkpoint_c"),
    }


async def synthesize_final_payload(state: ResearchWorkflowState) -> dict[str, Any]:
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

    blocked_modules = list(state.get("blocked_modules", []) or [])
    if blocked_modules:
        final_text = normalize_markdown_report(
            "\n\n".join(
                part for part in (
                    final_text,
                    "## 当前阻塞项\n\n" + _blocked_modules_summary(blocked_modules),
                ) if part
            )
        )

    task_id = str(state.get("task_id", "") or "")
    if task_id:
        memory = ResearchMemory(task_id)
        if memory.load_meta() is None:
            memory.init(str(state.get("query", "") or ""), str(state.get("report_profile", "") or ""))
        memory.save_final_report(final_text)
        _emit_stage_artifacts(
            task_id,
            stage_id="final_report",
            stage_title="最终研究输出",
            files=[
                _stage_file_ref(
                    task_id,
                    "final_report.md",
                    name="最终研究输出.md",
                    file_type="markdown",
                )
            ],
        )

    _safe_emit("step", "Synthesized final research output")
    return {
        "final_result": final_text,
        "workflow_trace": _append_trace(state, "synthesize_final"),
    }


async def synthesize_final_node(state: ResearchWorkflowState) -> dict[str, Any]:
    return await synthesize_final_payload(state)


def build_research_workflow_graph(config: ResearchConfig | None = None) -> Any:
    """构建科研工作流主图。"""
    cfg = config or ResearchConfig()
    graph = StateGraph(ResearchWorkflowState)

    # 节点职责保持单一：接收/规划/执行/聚合/评估/修订/终稿。
    graph.add_node("intake", intake_node, retry_policy=_workflow_llm_retry_policy())
    graph.add_node("planner", planner_node, retry_policy=_workflow_llm_retry_policy())
    graph.add_node("checkpoint_a", checkpoint_a_node)
    graph.add_node("dispatch_modules", dispatch_modules_node)
    graph.add_node("aggregate_draft", aggregate_draft_node, retry_policy=_workflow_llm_retry_policy())
    graph.add_node("evaluate_draft", evaluate_draft_node)
    graph.add_node("checkpoint_b", checkpoint_b_node)
    graph.add_node("optimize_modules", optimize_modules_node, retry_policy=_workflow_llm_retry_policy())
    graph.add_node("checkpoint_c", checkpoint_c_node)
    graph.add_node("synthesize_final", synthesize_final_node, retry_policy=_workflow_llm_retry_policy())

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
        budget = dict(state.get("budget", {}) or {})
        if budget.get("awaiting_user_decision") and str(budget.get("last_user_decision", "") or "") != "extend":
            return "synthesize_final"
        actionable_targets = _actionable_revision_targets(
            list(state.get("revision_targets", []) or []),
            dict(state.get("module_status", {}) or {}),
        )
        if not actionable_targets and state.get("blocked_modules"):
            return "synthesize_final"
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
