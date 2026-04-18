from __future__ import annotations

from typing import Any


def build_quality_report(
    *,
    format_name: str,
    operation: str,
    validated: bool,
    artifacts: list[dict[str, Any]],
    summary: str,
    stats: dict[str, Any],
    issues: list[dict[str, Any]],
    qa_fix_round: int,
    max_qa_fix_rounds: int,
    terminal_reason: str = "",
) -> dict[str, Any]:
    error_count = sum(1 for issue in issues if str(issue.get("severity", "") or "").lower() == "error")
    warning_count = sum(1 for issue in issues if str(issue.get("severity", "") or "").lower() == "warning")
    passed = error_count == 0
    if passed:
        status = "passed"
    elif qa_fix_round + 1 > max_qa_fix_rounds or terminal_reason:
        status = "hard_fail"
    else:
        status = "fixable"

    report = {
        "format": str(format_name or "").lower(),
        "operation": str(operation or "").lower(),
        "validated": bool(validated),
        "status": status,
        "passed": passed,
        "issue_count": len(issues),
        "error_count": error_count,
        "warning_count": warning_count,
        "artifact_count": len(artifacts or []),
        "summary": str(summary or "").strip(),
        "issues": list(issues or []),
        "qa_fix_round": int(qa_fix_round or 0),
        "max_qa_fix_rounds": int(max_qa_fix_rounds or 0),
        "stats_summary": _summarize_stats(stats),
    }
    if terminal_reason:
        report["terminal_reason"] = str(terminal_reason)
    return report


def summarize_quality_report(report: dict[str, Any] | None) -> dict[str, Any]:
    active = dict(report or {})
    stats = dict(active.get("stats_summary") or {})
    fidelity_deviations = list(active.get("fidelity_deviations") or [])
    summary = {
        "status": str(active.get("status", "") or ""),
        "passed": bool(active.get("passed", False)),
        "issue_count": int(active.get("issue_count", 0) or 0),
        "error_count": int(active.get("error_count", 0) or 0),
        "warning_count": int(active.get("warning_count", 0) or 0),
        "validated": bool(active.get("validated", False)),
    }
    for key in (
        "section_count",
        "sheet_count",
        "slide_count",
        "content_slide_count",
        "notes_slide_count",
        "transition_slide_count",
        "visual_slide_count",
        "text_only_slide_count",
        "layout_variety_count",
        "picture_count",
        "chart_count",
        "table_count",
    ):
        value = stats.get(key)
        if value is None:
            value = active.get(key)
        if value is not None:
            summary[key] = value
    if fidelity_deviations:
        summary["fidelity_deviation_count"] = len(fidelity_deviations)
    elif active.get("fidelity_deviation_count") is not None:
        summary["fidelity_deviation_count"] = int(active.get("fidelity_deviation_count", 0) or 0)
    if active.get("terminal_reason"):
        summary["terminal_reason"] = str(active.get("terminal_reason") or "")
    return summary


def quality_report_summary_lines(report: dict[str, Any] | None) -> list[str]:
    summary = summarize_quality_report(report)
    if not summary:
        return []
    lines = [
        f"质量状态: {summary.get('status', '')}",
        f"质量问题: {summary.get('issue_count', 0)} 个（error={summary.get('error_count', 0)}, warning={summary.get('warning_count', 0)}）",
    ]
    if summary.get("section_count") is not None:
        lines.append(f"关键统计: sections={summary.get('section_count')}")
    elif summary.get("slide_count") is not None:
        lines.append(
            "关键统计: "
            f"slides={summary.get('slide_count')}, "
            f"visual={summary.get('visual_slide_count', 0)}, "
            f"text_only={summary.get('text_only_slide_count', 0)}, "
            f"layout_variety={summary.get('layout_variety_count', 0)}"
        )
    elif summary.get("sheet_count") is not None:
        lines.append(f"关键统计: sheets={summary.get('sheet_count')}")
    if int(summary.get("fidelity_deviation_count", 0) or 0) > 0:
        lines.append(f"保真偏差: {summary.get('fidelity_deviation_count', 0)} 个")
    if summary.get("terminal_reason"):
        lines.append(f"质量终止原因: {summary.get('terminal_reason')}")
    return lines


def _summarize_stats(stats: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(stats, dict):
        return {}
    summary_keys = (
        "section_count",
        "sheet_count",
        "slide_count",
        "content_slide_count",
        "notes_slide_count",
        "transition_slide_count",
        "visual_slide_count",
        "text_only_slide_count",
        "layout_variety_count",
        "picture_count",
        "chart_count",
        "table_count",
        "qa_checks",
    )
    return {
        key: stats.get(key)
        for key in summary_keys
        if stats.get(key) is not None
    }
