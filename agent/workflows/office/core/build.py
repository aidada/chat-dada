from __future__ import annotations

import time
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.config import get_config

from core.models import build_chat_model
from deepagents import create_deep_agent
from agent.workflows.office.core.state import OfficeWorkflowState
from agent.workflows.office.strategies.base import OfficeFormatStrategy
from agent.workflows.office.tools import get_office_tools
from agent.hands.deepagents_backend import resolve_deepagents_runtime
from agent.platform.emit import safe_emit_progress_with_content as _safe_emit
from agent.platform.streaming import stream_nested_graph
from agent.runtime.cost_logging import append_stage_record, attach_partial_progress, update_completed_pages
from agent.tools.officecli import ALLOWED_DIR
from agent.tools.officecli_skill_loader import build_officecli_skill_bundle


class _OfficeStrictToolBindingMiddleware(AgentMiddleware[Any, Any, Any]):
    """Force strict tool binding only for Office-domain model calls."""

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler,
    ) -> ModelResponse[Any]:
        settings = dict(request.model_settings or {})
        settings["strict"] = True
        return await handler(request.override(model_settings=settings))


async def run_build_stage(
    state: OfficeWorkflowState,
    *,
    strategy: OfficeFormatStrategy,
    system_template: str,
    format_specific_guidance: str,
    office_model_role: str,
    subagents: list[dict[str, Any]],
) -> dict[str, Any]:
    _safe_emit("step", "Office: Sequential execution...")

    context_parts = [
        r["output"]
        for r in state.get("intermediate_results", [])
        if r.get("output")
    ]
    context = "\n\n---\n\n".join(context_parts[-3:]) if context_parts else ""
    latest_evaluation = (state.get("evaluations") or [])[-1] if state.get("evaluations") else {}
    latest_issues = latest_evaluation.get("issues", []) if isinstance(latest_evaluation, dict) else []
    qa_feedback = "\n".join(
        f"- {str(issue.get('message', '') or '').strip()}"
        for issue in latest_issues
        if str(issue.get("message", "") or "").strip()
    )

    source_files = list(state.get("allowed_source_files", []) or [])
    source_lines = "\n".join(f"- {item}" for item in source_files) if source_files else "- 无"
    format_hint = str(state.get("format", "") or state.get("format_hint", "") or "auto")
    operation = str(state.get("operation", "") or "create")
    runtime_target = str(state.get("runtime_target_hint", "") or "server")
    default_create_file = str(state.get("default_create_file", "") or "")
    requested_slide_count = _coerce_optional_positive_int(state.get("requested_slide_count"))
    build_batch_size = int(state.get("build_batch_size", 0) or 0) or 1
    cost_ledger = dict(state.get("cost_ledger") or {})
    deck_plan = dict(state.get("deck_plan") or {})
    task_profile = dict(state.get("task_profile") or {})
    merged_constraints = dict(task_profile.get("merged_constraints") or {})
    current_batch_index = int(state.get("current_batch_index", 0) or 0)
    repair_mode = bool(state.get("repair_mode"))

    skill_content = build_officecli_skill_bundle(
        str(state.get("goal", "") or ""),
        file_hint=str(state.get("file_hint", "") or default_create_file),
        format_hint=format_hint if format_hint != "auto" else None,
        operation_hint=operation,
    )
    phase_guidance = strategy.build_phase_guidance(
        plan=deck_plan,
        current_batch_index=current_batch_index,
        repair_mode=repair_mode,
        qa_feedback=qa_feedback,
    )
    system_prompt = system_template.format(
        format_hint=format_hint,
        operation=operation,
        runtime_target=runtime_target,
        default_create_file=default_create_file or "-",
        source_files_block=source_lines,
        format_specific_guidance=format_specific_guidance,
        phase_guidance=phase_guidance,
        skill_content=skill_content,
    )

    try:
        configurable = get_config().get("configurable", {}) or {}
    except Exception:
        configurable = {}
    task_id = str(state.get("task_id", "") or configurable.get("thread_id", "") or "office_domain")
    tools, backend = resolve_deepagents_runtime(
        domain="office",
        task_id=task_id,
        fallback_tools=list(get_office_tools()),
        configurable=configurable,
    )

    agent = create_deep_agent(
        model=build_chat_model(office_model_role),
        system_prompt=system_prompt,
        tools=tools,
        middleware=[_OfficeStrictToolBindingMiddleware()],
        subagents=subagents,
        backend=backend,
        checkpointer=False,
        name="office_sequential",
    )

    input_msg = "\n".join(
        strategy.build_input_sections(
            goal=str(state.get("goal", "") or ""),
            operation=operation,
            format_hint=format_hint,
            runtime_target=runtime_target,
            default_create_file=default_create_file,
            requested_slide_count=requested_slide_count,
            build_batch_size=build_batch_size,
            source_files=source_files,
            context=context,
            qa_feedback=qa_feedback,
            plan=deck_plan,
            current_batch_index=current_batch_index,
            repair_mode=repair_mode,
            merged_constraints=merged_constraints,
        )
    )

    office_constraints = {
        "allowed_source_files": source_files,
        "allowed_output_dir": str(ALLOWED_DIR),
        "runtime_target": runtime_target,
        "default_create_file": default_create_file,
    }
    inner_limit = int(
        state.get("inner_recursion_limit", 0) or 0
    )
    build_started_at = time.perf_counter()
    try:
        response = await stream_nested_graph(
            agent,
            {"messages": [HumanMessage(content=input_msg)]},
            config={
                "recursion_limit": inner_limit,
                "configurable": {
                    "nested_recursion_limit": inner_limit,
                    "office_constraints": office_constraints,
                    "office_cost_stage": "build",
                },
            },
            extra_payload={
                "nested_graph": "office_sequential",
                "strategy": "sequential",
                "source": "office_workflow",
            },
        )
        output = _extract_last_ai_text(response)
    except GraphRecursionError:
        elapsed_ms = int((time.perf_counter() - build_started_at) * 1000)
        partial_progress = _build_partial_progress(state, reason="inner_recursion_limit")
        cost_ledger = update_completed_pages(
            cost_ledger,
            completed_pages=int(partial_progress.get("completed_pages", 0) or 0),
        )
        cost_ledger = attach_partial_progress(cost_ledger, partial_progress=partial_progress)
        cost_ledger = append_stage_record(
            cost_ledger,
            stage="build",
            status="bounded_failure",
            elapsed_ms=elapsed_ms,
            metadata={
                "inner_recursion_limit": inner_limit,
                "build_batch_size": build_batch_size,
                "partial_progress": partial_progress,
            },
        )
        output = (
            f"Office 任务已中止：内层 agent 超过 {inner_limit} 步仍未收敛，"
            "疑似重复工具调用。请检查 officecli 返回或提示词收敛规则。"
        )
        _safe_emit("step", output)
        return {
            "intermediate_results": [{
                "strategy": "sequential",
                "output": output,
                "bounded_failure": True,
                "reason": "inner_recursion_limit",
            }],
            "evaluations": [{
                "passed": False,
                "confidence": 0.0,
                "issues": [{
                    "severity": "error",
                    "message": "Office inner agent hit recursion limit",
                    "metadata": {"limit": inner_limit},
                }],
            }],
            "final_result": output,
            "confidence": 0.0,
            "terminal_status": "bounded_failure",
            "terminal_reason": "inner_recursion_limit",
            "cost_ledger": cost_ledger,
            "partial_progress": partial_progress,
            "current_stage": "finalize",
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - build_started_at) * 1000)
        partial_progress = _build_partial_progress(state, reason="inner_agent_exception")
        cost_ledger = update_completed_pages(
            cost_ledger,
            completed_pages=int(partial_progress.get("completed_pages", 0) or 0),
        )
        cost_ledger = attach_partial_progress(cost_ledger, partial_progress=partial_progress)
        cost_ledger = append_stage_record(
            cost_ledger,
            stage="build",
            status="error",
            elapsed_ms=elapsed_ms,
            metadata={
                "error": str(exc),
                "build_batch_size": build_batch_size,
                "partial_progress": partial_progress,
            },
        )
        output = f"Office 任务失败：内层 agent 执行异常：{exc}"
        _safe_emit("step", output)
        return {
            "intermediate_results": [{
                "strategy": "sequential",
                "output": output,
                "bounded_failure": True,
                "reason": "inner_agent_exception",
            }],
            "evaluations": [{
                "passed": False,
                "confidence": 0.0,
                "issues": [{
                    "severity": "error",
                    "message": "Office inner agent raised an exception",
                    "metadata": {"error": str(exc)},
                }],
            }],
            "final_result": output,
            "confidence": 0.0,
            "terminal_status": "error",
            "terminal_reason": "inner_agent_exception",
            "cost_ledger": cost_ledger,
            "partial_progress": partial_progress,
            "current_stage": "finalize",
        }

    elapsed_ms = int((time.perf_counter() - build_started_at) * 1000)
    cost_ledger = append_stage_record(
        cost_ledger,
        stage="build",
        status="ok",
        elapsed_ms=elapsed_ms,
        metadata={
            "build_batch_size": build_batch_size,
            "requested_slide_count": requested_slide_count,
        },
    )
    _safe_emit("step", f"Office: Sequential done ({len(output)} chars)")
    completed_pages = int(state.get("completed_pages", 0) or 0)
    advanced = strategy.advance_after_build(
        plan=deck_plan,
        current_batch_index=current_batch_index,
        repair_mode=repair_mode,
        completed_pages=completed_pages,
    )
    next_completed_pages = int(advanced.get("completed_pages", completed_pages) or completed_pages)
    next_batch_index = int(advanced.get("current_batch_index", current_batch_index) or current_batch_index)
    partial_progress = _build_partial_progress(
        {
            **state,
            "completed_pages": next_completed_pages,
            "current_batch_index": next_batch_index,
            "current_stage": str(advanced.get("next_stage", "qa_fix") or "qa_fix"),
        }
    )
    cost_ledger = update_completed_pages(cost_ledger, completed_pages=next_completed_pages)
    cost_ledger = attach_partial_progress(cost_ledger, partial_progress=partial_progress)
    return {
        "intermediate_results": [{"strategy": "sequential", "output": output}],
        "cost_ledger": cost_ledger,
        "current_batch_index": next_batch_index,
        "completed_pages": next_completed_pages,
        "repair_mode": False,
        "partial_progress": partial_progress,
        "current_stage": str(advanced.get("next_stage", "qa_fix") or "qa_fix"),
    }


def _extract_last_ai_text(response: Any) -> str:
    messages = response.get("messages", []) if isinstance(response, dict) else []
    for msg in reversed(messages):
        content = getattr(msg, "content", "")
        if content:
            return str(content)
    return ""


def _build_partial_progress(state: OfficeWorkflowState | dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    active = state or {}
    plan = dict(active.get("deck_plan") or {})
    batches = list(plan.get("batches") or [])
    current_batch_index = int(active.get("current_batch_index", 0) or 0)
    current_batch = batches[current_batch_index] if 0 <= current_batch_index < len(batches) else None
    requested_pages = _coerce_optional_positive_int(active.get("requested_slide_count"))
    progress = {
        "stage": str(active.get("current_stage", "") or "build"),
        "completed_pages": int(active.get("completed_pages", 0) or 0),
        "current_batch_index": current_batch_index,
        "total_batches": len(batches),
        "build_batch_size": int(active.get("build_batch_size", 0) or 0),
    }
    if requested_pages is not None:
        progress["requested_pages"] = requested_pages
    if current_batch is not None:
        progress["current_batch_slide_range"] = [
            int(current_batch.get("slide_start", 0) or 0),
            int(current_batch.get("slide_end", 0) or 0),
        ]
        progress["current_batch_titles"] = list(current_batch.get("slide_titles", []) or [])
    if reason:
        progress["reason"] = reason
    return progress


def _coerce_optional_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
