from __future__ import annotations

from agent.domains.zero_report.schemas import (
    ActionMatrix,
    IncidentFactSet,
    RootCauseNode,
    RootCauseTree,
    Timeline,
    ZeroReportDraft,
)


def _render_cause_tree(node: RootCauseNode, depth: int = 0) -> list[str]:
    indent = "  " * depth
    lines = [f"{indent}- {node.label}"]
    for child in node.children:
        lines.extend(_render_cause_tree(child, depth + 1))
    return lines


def render_zero_report_markdown(
    facts: IncidentFactSet,
    timeline: Timeline,
    root_cause_tree: RootCauseTree,
    action_matrix: ActionMatrix,
    draft: ZeroReportDraft,
) -> str:
    timeline_lines = [f"- {event.timestamp}: {event.detail}" for event in timeline.events]
    cause_lines = _render_cause_tree(root_cause_tree.root)
    action_lines = [f"- {item.owner} @ {item.due_date}: {item.action}" for item in action_matrix.items]
    return "\n".join(
        [
            f"# {draft.title or facts.title or 'Zero Report'}",
            "",
            "## 事件摘要",
            facts.summary,
            "",
            "## 时间线",
            *timeline_lines,
            "",
            "## 根因",
            *cause_lines,
            "",
            "## 整改矩阵",
            *action_lines,
            "",
            "## 结论",
            draft.executive_summary,
            "",
            "## 整改计划",
            draft.remediation_plan,
        ]
    ).strip()


def render_zero_report_pptx(
    facts: IncidentFactSet,
    timeline: Timeline,
    root_cause_tree: RootCauseTree,
    action_matrix: ActionMatrix,
    draft: ZeroReportDraft,
    output_path: str,
) -> str:
    """Render a zero report as .pptx via the shared PPT capability."""
    from agent.capabilities.ppt_capability import markdown_to_deck, render_deck_to_pptx

    md = render_zero_report_markdown(facts, timeline, root_cause_tree, action_matrix, draft)
    title = draft.title or facts.title or "Zero Report"
    deck = markdown_to_deck(title, md)
    return render_deck_to_pptx(deck, output_path)

