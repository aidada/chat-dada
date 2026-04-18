from __future__ import annotations

from agent.runtime.cost_logging import (
    attach_partial_progress,
    attach_quality_summary,
    build_failure_diagnostics,
    init_cost_ledger,
    merge_llm_usage_into_ledger,
    merge_tool_events_into_ledger,
    summarize_cost_ledger,
)
from agent.workflows.office.core.quality_report import (
    build_quality_report,
    quality_report_summary_lines,
    summarize_quality_report,
)


def test_merge_llm_usage_into_ledger_records_model_cost() -> None:
    ledger = init_cost_ledger(task_id="t1", domain="office", requested_pages=10)
    merged = merge_llm_usage_into_ledger(
        ledger,
        llm_usage=[
            {
                "model": "test-model",
                "role": "orchestrator",
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
                "calls": 2,
            }
        ],
        estimate_cost=lambda **_: 0.123,
    )

    summary = summarize_cost_ledger(merged)
    assert summary["model_cost_usd"] == 0.123
    assert summary["total_cost_usd"] == 0.123
    assert summary["call_records"][0]["call_type"] == "model"


def test_merge_tool_events_into_ledger_records_tool_details_and_diagnostics() -> None:
    ledger = init_cost_ledger(task_id="t1", domain="office", requested_pages=10)
    events = [
        {
            "type": "tool.completed",
            "payload": {
                "stage": "build",
                "name": "officecli_batch",
                "execution_time_ms": 1200,
                "output": '{"success": true, "command": "officecli add deck.pptx / --type slide", "kind": "success", "message": "Added slide at /slide[8]"}',
            },
        },
        {
            "type": "tool.failed",
            "payload": {
                "stage": "qa_fix",
                "name": "officecli",
                "error": 'OfficeCLI view requires a non-empty "mode" parameter.',
                "output": '{"success": false, "command": "officecli view deck.pptx", "kind": "fatal_error", "message": "OfficeCLI view requires a non-empty \\"mode\\" parameter."}',
            },
        },
    ]
    merged = merge_tool_events_into_ledger(ledger, events=events)
    summary = summarize_cost_ledger(merged)
    diagnostics = build_failure_diagnostics(events)

    assert len(summary["call_records"]) == 2
    assert summary["call_records"][0]["stage"] == "build"
    assert summary["call_records"][1]["result_kind"] == "fatal_error"
    assert diagnostics["completed_pages"] == 8
    assert diagnostics["current_stage"] == "build"
    assert diagnostics["last_successful_tool"]["command"] == "officecli add deck.pptx / --type slide"


def test_quality_report_summary_and_lines_include_key_stats() -> None:
    report = build_quality_report(
        format_name="pptx",
        operation="create",
        validated=False,
        artifacts=[{"name": "deck.pptx"}],
        summary="质量未达标",
        stats={
            "slide_count": 6,
            "visual_slide_count": 2,
            "text_only_slide_count": 1,
            "layout_variety_count": 2,
        },
        issues=[{"severity": "error", "message": "缺少 transition"}],
        qa_fix_round=1,
        max_qa_fix_rounds=2,
        terminal_reason="qa_fix_round_exhausted",
    )

    summary = summarize_quality_report(report)
    lines = quality_report_summary_lines(report)

    assert summary["status"] == "hard_fail"
    assert summary["slide_count"] == 6
    assert summary["error_count"] == 1
    assert any("质量状态: hard_fail" == line for line in lines)
    assert any("slides=6" in line for line in lines)


def test_quality_report_summary_includes_fidelity_deviations() -> None:
    report = build_quality_report(
        format_name="pptx",
        operation="create",
        validated=True,
        artifacts=[{"name": "deck.pptx"}],
        summary="done",
        stats={"slide_count": 6},
        issues=[],
        qa_fix_round=0,
        max_qa_fix_rounds=2,
        terminal_reason="",
    )
    report["fidelity_deviations"] = [{"kind": "style_deviation", "message": "theme fallback"}]

    summary = summarize_quality_report(report)

    assert summary["fidelity_deviation_count"] == 1


def test_quality_report_summary_lines_render_from_merged_summary_payload() -> None:
    lines = quality_report_summary_lines(
        {
            "status": "hard_fail",
            "passed": False,
            "issue_count": 1,
            "error_count": 1,
            "warning_count": 0,
            "slide_count": 6,
            "visual_slide_count": 4,
            "text_only_slide_count": 1,
            "layout_variety_count": 3,
            "fidelity_deviation_count": 1,
            "terminal_reason": "inner_recursion_limit",
        }
    )

    assert any("slides=6" in line for line in lines)
    assert any("保真偏差: 1 个" == line for line in lines)
    assert any("质量终止原因: inner_recursion_limit" == line for line in lines)


def test_quality_report_summary_lines_return_empty_for_empty_input() -> None:
    assert quality_report_summary_lines(None) == []
    assert quality_report_summary_lines({}) == []


def test_cost_ledger_summary_includes_quality_report_summary() -> None:
    ledger = init_cost_ledger(task_id="t1", domain="office", requested_pages=6)
    report = build_quality_report(
        format_name="pptx",
        operation="create",
        validated=False,
        artifacts=[{"name": "deck.pptx"}],
        summary="质量未达标",
        stats={"slide_count": 6},
        issues=[{"severity": "error", "message": "缺少 transition"}],
        qa_fix_round=1,
        max_qa_fix_rounds=2,
    )
    merged = attach_quality_summary(ledger, quality_report_summary=summarize_quality_report(report))
    summary = summarize_cost_ledger(merged)

    assert summary["quality_report_summary"]["status"] == "fixable"
    assert summary["quality_report_summary"]["slide_count"] == 6


def test_cost_ledger_summary_includes_partial_progress() -> None:
    ledger = init_cost_ledger(task_id="t1", domain="office", requested_pages=10)
    merged = attach_partial_progress(
        ledger,
        partial_progress={
            "stage": "build",
            "completed_pages": 6,
            "requested_pages": 10,
            "current_batch_index": 2,
        },
    )
    summary = summarize_cost_ledger(merged)

    assert summary["completed_pages"] == 6
    assert summary["partial_progress"]["stage"] == "build"
    assert summary["partial_progress"]["current_batch_index"] == 2
