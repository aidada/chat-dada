from __future__ import annotations

from typing import Any

from agent.workflows.office.core.quality_report import build_quality_report, summarize_quality_report
from agent.workflows.office.core.state import OfficeWorkflowState
from agent.workflows.office.result_utils import coerce_office_operation, extract_office_result_json
from agent.workflows.office.strategies.base import OfficeFormatStrategy
from agent.runtime.cost_logging import append_stage_record, attach_partial_progress, attach_quality_summary, update_completed_pages


def _attach_fidelity_deviations(
    report: dict[str, Any],
    state: OfficeWorkflowState,
) -> dict[str, Any]:
    deviations = state.get("fidelity_deviations")
    if not isinstance(deviations, list) or not deviations:
        return report
    decorated = dict(report)
    decorated["fidelity_deviations"] = list(deviations)
    return decorated


def _extract_completed_units(stats: dict[str, Any], fallback: int) -> int:
    if not isinstance(stats, dict):
        return fallback
    for key in ("slide_count", "sheet_count", "section_count"):
        try:
            value = int(stats.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return fallback


def run_qa_fix_stage(
    state: OfficeWorkflowState,
    *,
    strategy: OfficeFormatStrategy,
) -> dict[str, Any]:
    terminal_status = str(state.get("terminal_status", "") or "")
    cost_ledger = dict(state.get("cost_ledger") or {})
    if terminal_status:
        evaluation = {
            "passed": False,
            "confidence": 0.0,
            "issues": [{
                "severity": "error",
                "message": str(state.get("terminal_reason", terminal_status)),
                "metadata": {"terminal_status": terminal_status},
            }],
        }
        cost_ledger = append_stage_record(
            cost_ledger,
            stage="qa_fix",
            status="blocked",
            elapsed_ms=0,
            metadata={"terminal_status": terminal_status},
        )
        quality_report = _attach_fidelity_deviations(
            build_quality_report(
            format_name=str(state.get("format", "") or state.get("format_hint", "") or ""),
            operation=str(state.get("operation", "") or ""),
            validated=False,
            artifacts=[],
            summary=str(state.get("final_result", "") or ""),
            stats={},
            issues=evaluation["issues"],
            qa_fix_round=int(state.get("qa_fix_round", 0) or 0),
            max_qa_fix_rounds=int(state.get("max_qa_fix_rounds", 0) or 0),
            terminal_reason=str(state.get("terminal_reason", terminal_status) or terminal_status),
            ),
            state,
        )
        return {
            "evaluations": state.get("evaluations") or [evaluation],
            "final_result": str(state.get("final_result", "") or ""),
            "confidence": float(state.get("confidence", 0.0) or 0.0),
            "cost_ledger": cost_ledger,
            "current_stage": "finalize",
            "quality_report": quality_report,
        }

    results = state.get("intermediate_results", [])
    if not results:
        cost_ledger = append_stage_record(
            cost_ledger,
            stage="qa_fix",
            status="error",
            elapsed_ms=0,
            metadata={"reason": "no_strategy_output"},
        )
        return {
            "evaluations": [{
                "passed": False,
                "confidence": 0.0,
                "issues": [{"severity": "error", "message": "策略未产出任何输出"}],
            }],
            "cost_ledger": cost_ledger,
            "current_stage": "finalize",
            "terminal_status": "error",
            "terminal_reason": "no_strategy_output",
            "quality_report": _attach_fidelity_deviations(
                build_quality_report(
                    format_name=str(state.get("format", "") or state.get("format_hint", "") or ""),
                    operation=str(state.get("operation", "") or ""),
                    validated=False,
                    artifacts=[],
                    summary="",
                    stats={},
                    issues=[{"severity": "error", "message": "策略未产出任何输出"}],
                    qa_fix_round=int(state.get("qa_fix_round", 0) or 0),
                    max_qa_fix_rounds=int(state.get("max_qa_fix_rounds", 0) or 0),
                    terminal_reason="no_strategy_output",
                ),
                state,
            ),
        }

    output = str(results[-1].get("output", "") or "")
    if not output:
        cost_ledger = append_stage_record(
            cost_ledger,
            stage="qa_fix",
            status="error",
            elapsed_ms=0,
            metadata={"reason": "empty_strategy_output"},
        )
        return {
            "evaluations": [{
                "passed": False,
                "confidence": 0.0,
                "issues": [{"severity": "error", "message": "策略未产出任何输出"}],
            }],
            "cost_ledger": cost_ledger,
            "current_stage": "finalize",
            "terminal_status": "error",
            "terminal_reason": "empty_strategy_output",
            "quality_report": _attach_fidelity_deviations(
                build_quality_report(
                    format_name=str(state.get("format", "") or state.get("format_hint", "") or ""),
                    operation=str(state.get("operation", "") or ""),
                    validated=False,
                    artifacts=[],
                    summary="",
                    stats={},
                    issues=[{"severity": "error", "message": "策略未产出任何输出"}],
                    qa_fix_round=int(state.get("qa_fix_round", 0) or 0),
                    max_qa_fix_rounds=int(state.get("max_qa_fix_rounds", 0) or 0),
                    terminal_reason="empty_strategy_output",
                ),
                state,
            ),
        }

    meta = extract_office_result_json(output)
    if meta is None:
        cost_ledger = append_stage_record(
            cost_ledger,
            stage="qa_fix",
            status="error",
            elapsed_ms=0,
            metadata={"reason": "missing_structured_office_json"},
        )
        return {
            "evaluations": [{
                "passed": False,
                "confidence": 0.0,
                "issues": [{
                    "severity": "error",
                    "message": "最终回复缺少结构化 Office JSON 结果",
                }],
            }],
            "cost_ledger": cost_ledger,
            "current_stage": "finalize",
            "terminal_status": "error",
            "terminal_reason": "missing_structured_office_json",
            "quality_report": _attach_fidelity_deviations(
                build_quality_report(
                    format_name=str(state.get("format", "") or state.get("format_hint", "") or ""),
                    operation=str(state.get("operation", "") or ""),
                    validated=False,
                    artifacts=[],
                    summary=output,
                    stats={},
                    issues=[{"severity": "error", "message": "最终回复缺少结构化 Office JSON 结果"}],
                    qa_fix_round=int(state.get("qa_fix_round", 0) or 0),
                    max_qa_fix_rounds=int(state.get("max_qa_fix_rounds", 0) or 0),
                    terminal_reason="missing_structured_office_json",
                ),
                state,
            ),
        }

    operation = coerce_office_operation(meta.get("operation") or state.get("operation"))
    validated = bool(meta.get("validated", False))
    artifacts = meta.get("artifacts") if isinstance(meta.get("artifacts"), list) else []
    summary = str(meta.get("summary", "") or "").strip()
    stats = meta.get("stats") if isinstance(meta.get("stats"), dict) else {}
    plan = state.get("deck_plan") if isinstance(state.get("deck_plan"), dict) else None
    task_profile = state.get("task_profile") if isinstance(state.get("task_profile"), dict) else {}
    merged_constraints = (
        task_profile.get("merged_constraints")
        if isinstance(task_profile.get("merged_constraints"), dict)
        else None
    )
    issues: list[dict[str, Any]] = []

    if operation != "inspect" and not artifacts:
        issues.append({"severity": "error", "message": "写入型 Office 任务缺少 artifacts"})
    if bool(state.get("write_required")) and not validated:
        issues.append({"severity": "error", "message": "写入型 Office 任务未完成 validate"})
    if operation == "inspect" and not summary and not output.strip():
        issues.append({"severity": "error", "message": "inspect 任务缺少有效总结"})
    issues.extend(
        strategy.evaluate_quality_stats(
            operation=operation,
            stats=stats,
            plan=plan,
            merged_constraints=merged_constraints,
            result_meta=meta,
        )
    )

    passed = not any(issue["severity"] == "error" for issue in issues)
    quality_report = _attach_fidelity_deviations(
        build_quality_report(
            format_name=str(state.get("format", "") or state.get("format_hint", "") or ""),
            operation=operation,
            validated=validated,
            artifacts=list(artifacts or []),
            summary=summary,
            stats=stats,
            issues=issues,
            qa_fix_round=int(state.get("qa_fix_round", 0) or 0),
            max_qa_fix_rounds=int(state.get("max_qa_fix_rounds", 0) or 0),
        ),
        state,
    )
    evaluation = {
        "passed": passed,
        "confidence": 0.9 if passed else 0.0,
        "issues": issues,
    }
    quality_summary = summarize_quality_report(quality_report)
    completed_pages = _extract_completed_units(stats, int(state.get("completed_pages", 0) or 0))
    cost_ledger = update_completed_pages(cost_ledger, completed_pages=completed_pages)
    cost_ledger = attach_quality_summary(cost_ledger, quality_report_summary=quality_summary)

    if passed:
        cost_ledger = append_stage_record(
            cost_ledger,
            stage="qa_fix",
            status="passed",
            elapsed_ms=0,
            metadata={"issue_count": len(issues), "quality_status": quality_summary.get("status", "")},
        )
        return {
            "evaluations": [evaluation],
            "final_result": output,
            "confidence": 0.9,
            "cost_ledger": cost_ledger,
            "current_stage": "finalize",
            "quality_report": quality_report,
        }

    cost_ledger = append_stage_record(
        cost_ledger,
        stage="qa_fix",
        status="fixable",
        elapsed_ms=0,
        metadata={"issue_count": len(issues), "quality_status": quality_summary.get("status", "")},
    )
    qa_fix_round = int(state.get("qa_fix_round", 0) or 0)
    max_qa_fix_rounds = int(state.get("max_qa_fix_rounds", 2) or 2)
    next_round = qa_fix_round + 1
    if next_round > max_qa_fix_rounds:
        requested_pages = _coerce_optional_positive_int(state.get("requested_slide_count"))
        partial_progress = {
            "stage": "qa_fix",
            "completed_pages": completed_pages,
            "qa_fix_round": next_round,
            "max_qa_fix_rounds": max_qa_fix_rounds,
            "reason": "qa_fix_round_exhausted",
        }
        if requested_pages is not None:
            partial_progress["requested_pages"] = requested_pages
        cost_ledger = attach_partial_progress(cost_ledger, partial_progress=partial_progress)
        return {
            "evaluations": [evaluation],
            "confidence": 0.0,
            "cost_ledger": cost_ledger,
            "current_stage": "finalize",
            "terminal_status": "quality_gate_failed",
            "terminal_reason": "qa_fix_round_exhausted",
            "final_result": "Office QA 未通过：修复轮次已耗尽，任务在 qa_fix 阶段停止。",
            "partial_progress": partial_progress,
            "quality_report": _attach_fidelity_deviations(
                build_quality_report(
                    format_name=str(state.get("format", "") or state.get("format_hint", "") or ""),
                    operation=operation,
                    validated=validated,
                    artifacts=list(artifacts or []),
                    summary=summary,
                    stats=stats,
                    issues=issues,
                    qa_fix_round=next_round,
                    max_qa_fix_rounds=max_qa_fix_rounds,
                    terminal_reason="qa_fix_round_exhausted",
                ),
                state,
            ),
        }

    requested_pages = _coerce_optional_positive_int(state.get("requested_slide_count"))
    partial_progress = {
        "stage": "build",
        "completed_pages": completed_pages,
        "qa_fix_round": next_round,
        "max_qa_fix_rounds": max_qa_fix_rounds,
        "reason": "quality_gate_fixable",
    }
    if requested_pages is not None:
        partial_progress["requested_pages"] = requested_pages
    cost_ledger = attach_partial_progress(cost_ledger, partial_progress=partial_progress)
    return {
        "evaluations": [evaluation],
        "confidence": 0.0,
        "cost_ledger": cost_ledger,
        "current_stage": "build",
        "repair_mode": True,
        "qa_fix_round": next_round,
        "partial_progress": partial_progress,
        "quality_report": quality_report,
    }


def _coerce_optional_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
