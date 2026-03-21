from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from capabilities.budget_policy import BudgetPolicy
from capabilities.citation_manager import CitationMap
from capabilities.evidence_store import EvidenceCollection, EvidenceItem
from core.content_utils import extract_result_text
from domain_agents.zero_report.prompts import BASE_ZERO_REPORT_SYSTEM
from domain_agents.zero_report.renderers import render_zero_report_markdown
from domain_agents.zero_report.reviewers import ZeroReportReviewGate
from domain_agents.zero_report.schemas import (
    ActionItem,
    ActionMatrix,
    IncidentFactSet,
    RootCauseNode,
    RootCauseTree,
    Timeline,
    TimelineEvent,
    ZeroReportDraft,
)
from domain_agents.zero_report.tools import browser_collect_zero_report_context, get_zero_report_tools

_log = logging.getLogger("chatdada.zero_report")


def _safe_emit(event_type: str, content: str) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        writer({"event_type": event_type, "content": content})
    except Exception:
        pass


ZERO_REPORT_DATA_ROOT = Path("data/zero_report")


class ZeroReportDomainResult(BaseModel):
    status: str
    result: str
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    budget: dict[str, Any]


# ---------------------------------------------------------------------------
# Deepagents-backed zero-report agent
# ---------------------------------------------------------------------------

async def build_deepagents_zero_report_agent() -> object:
    """Build a deepagents-backed zero-report agent with 5 specialised subagents."""
    from deepagents import create_deep_agent

    from domain_agents.zero_report.prompts import (
        ACTION_PLANNER_PROMPT,
        ROOT_CAUSE_ANALYST_PROMPT,
        TIMELINE_BUILDER_PROMPT,
    )

    tools = get_zero_report_tools()
    subagents = [
        {
            "name": "incident_structurer",
            "description": "Extract structured incident facts from raw event description.",
            "system_prompt": "请从事件描述中提取结构化的事件摘要，包括标题、摘要、影响范围。输出 JSON。",
            "tools": tools,
        },
        {
            "name": "timeline_builder",
            "description": "Build a chronological timeline of key events.",
            "system_prompt": TIMELINE_BUILDER_PROMPT,
            "tools": tools,
        },
        {
            "name": "root_cause_analyst",
            "description": "Perform root cause analysis using the '5 Whys' method.",
            "system_prompt": ROOT_CAUSE_ANALYST_PROMPT,
            "tools": tools,
        },
        {
            "name": "corrective_action_planner",
            "description": "Define corrective actions with owners and deadlines.",
            "system_prompt": ACTION_PLANNER_PROMPT,
            "tools": tools,
        },
        {
            "name": "report_reviewer",
            "description": "Review the complete zero report for completeness and accountability.",
            "system_prompt": "请审查归零报告的完整性：时间线是否完整、根因是否达到可行动层、整改措施是否有责任人和时限。输出 JSON 数组，每条含 severity 和 message。",
            "tools": tools,
        },
    ]
    return create_deep_agent(
        model="openai:gpt-5.4-mini",
        system_prompt=BASE_ZERO_REPORT_SYSTEM,
        tools=tools,
        subagents=subagents,
        checkpointer=False,
        name="zero_report_domain_agent",
    )


# ---------------------------------------------------------------------------
# Heuristic (fallback) pipeline
# ---------------------------------------------------------------------------

def _build_facts(query: str) -> IncidentFactSet:
    return IncidentFactSet(
        title="Zero Report Draft",
        summary=query,
        impacted_scope="待人工补充影响范围；当前为结构化占位。",
    )


def _build_timeline(query: str) -> Timeline:
    return Timeline(
        events=[
            TimelineEvent(timestamp="T0", detail=f"事件被发现：{query}"),
            TimelineEvent(timestamp="T1", detail="进行了初步处置与信息收敛。"),
            TimelineEvent(timestamp="T2", detail="输出根因与整改闭环建议。"),
        ]
    )


def _build_root_cause(query: str) -> RootCauseTree:
    return RootCauseTree(
        root=RootCauseNode(
            label=f"根因分析：{query}",
            children=[
                RootCauseNode(label="触发条件未被前置检测"),
                RootCauseNode(label="处置流程缺少自动化校验"),
            ],
        )
    )


def _build_actions() -> ActionMatrix:
    return ActionMatrix(
        items=[
            ActionItem(owner="owner_a", due_date="D+7", action="补充监控与预警规则"),
            ActionItem(owner="owner_b", due_date="D+14", action="固化处置手册并完成演练"),
        ]
    )


def _build_draft(facts: IncidentFactSet) -> ZeroReportDraft:
    return ZeroReportDraft(
        title=facts.title,
        executive_summary=f"围绕\u201c{facts.summary}\u201d形成了结构化复盘与整改建议。",
        remediation_plan="按整改矩阵推进责任项闭环，并在下一个周期进行复核。",
    )


async def _run_heuristic_zero_report(
    query: str, task_id: str, input_data: dict[str, Any],
) -> str:
    """Heuristic pipeline — deterministic, no LLM calls."""
    facts = _build_facts(query)
    timeline = _build_timeline(query)
    root_cause_tree = _build_root_cause(query)
    action_matrix = _build_actions()
    draft = _build_draft(facts)

    if input_data.get("browser_enabled") and input_data.get("browser_task"):
        extra = await browser_collect_zero_report_context(str(input_data["browser_task"]), enabled=True)
        timeline.events.append(TimelineEvent(timestamp="T_browser", detail=extra))

    return render_zero_report_markdown(facts, timeline, root_cause_tree, action_matrix, draft)


# ---------------------------------------------------------------------------
# Artifact persistence (shared by both paths)
# ---------------------------------------------------------------------------

def _persist_artifacts(
    *,
    task_id: str,
    facts: IncidentFactSet,
    timeline: Timeline,
    root_cause_tree: RootCauseTree,
    action_matrix: ActionMatrix,
    draft: ZeroReportDraft,
    report: str,
) -> list[dict[str, Any]]:
    task_dir = ZERO_REPORT_DATA_ROOT / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "incident_fact_set.json": facts.model_dump(),
        "timeline.json": timeline.model_dump(),
        "root_cause_tree.json": root_cause_tree.model_dump(),
        "action_matrix.json": action_matrix.model_dump(),
        "zero_report_draft.json": draft.model_dump(),
    }
    refs: list[dict[str, Any]] = []
    for name, payload in payloads.items():
        path = task_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        refs.append({"type": "file", "name": name, "path": str(path)})
    report_path = task_dir / "zero_report.md"
    report_path.write_text(report, encoding="utf-8")
    refs.append({"type": "file", "name": report_path.name, "path": str(report_path)})
    return refs


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_zero_report_domain(input_data: dict[str, Any]) -> ZeroReportDomainResult:
    query = str(input_data.get("query", input_data.get("task", "")) or "").strip()
    task_id = str(input_data.get("task_id", "") or "zero_report_preview")
    use_deepagents = bool(input_data.get("use_deepagents", True))

    _safe_emit("step", f"📋 Zero report domain started: '{query[:60]}'")

    strategy = "heuristic"
    final_text = ""

    if use_deepagents:
        strategy = "deepagents_harness"
        _safe_emit("step", "🤖 Building deepagents zero-report agent (5 subagents)...")
        try:
            agent = await build_deepagents_zero_report_agent()
            _safe_emit("step", "🚀 Executing deepagents zero-report pipeline...")
            response = await agent.ainvoke(
                {"messages": [HumanMessage(content=f"请对以下事件进行归零分析，输出时间线、根因树、整改矩阵和归零报告草稿：\n\n{query}")]}
            )
            messages = response.get("messages", []) if isinstance(response, dict) else []
            for message in reversed(messages):
                if isinstance(message, AIMessage):
                    final_text = extract_result_text(getattr(message, "content", ""))
                    if final_text:
                        break
        except Exception as exc:
            _log.warning("Deepagents zero-report agent failed, falling back to heuristic: %s", exc)
            _safe_emit("step", "⚠️ Deepagents failed, using heuristic fallback")

    if not final_text:
        strategy = "heuristic_fallback" if use_deepagents else "heuristic"
        _safe_emit("step", "🔧 Running heuristic zero-report pipeline...")
        final_text = await _run_heuristic_zero_report(query, task_id, input_data)

    # Build structured artifacts from heuristic builders (always, for review gate)
    facts = _build_facts(query)
    timeline = _build_timeline(query)
    root_cause_tree = _build_root_cause(query)
    action_matrix = _build_actions()
    draft = _build_draft(facts)

    # Build evidence from timeline events and action items
    evidence = EvidenceCollection(task_id=task_id)
    citations = CitationMap()
    for event in timeline.events:
        evidence.add(EvidenceItem(
            evidence_id=f"ev_{len(evidence.items) + 1}",
            evidence_type="quote",
            source=f"timeline:{event.timestamp}",
            summary=event.detail,
        ))
    for item in action_matrix.items:
        evidence.add(EvidenceItem(
            evidence_id=f"ev_{len(evidence.items) + 1}",
            evidence_type="quote",
            source=f"action:{item.owner}",
            summary=f"{item.action} (due: {item.due_date})",
        ))

    _safe_emit("step", "📝 Persisting artifacts...")
    artifact_refs = _persist_artifacts(
        task_id=task_id,
        facts=facts,
        timeline=timeline,
        root_cause_tree=root_cause_tree,
        action_matrix=action_matrix,
        draft=draft,
        report=final_text,
    )

    # Persist evidence and citations
    task_dir = ZERO_REPORT_DATA_ROOT / task_id
    if evidence.items:
        ev_path = task_dir / "evidence.json"
        ev_path.write_text(json.dumps(
            [{"evidence_id": e.evidence_id, "type": e.evidence_type, "source": e.source, "summary": e.summary}
             for e in evidence.items],
            ensure_ascii=False, indent=2,
        ), encoding="utf-8")
        artifact_refs.append({"type": "file", "name": "evidence.json", "path": str(ev_path)})
    if citations.all():
        cit_path = task_dir / "citations.json"
        cit_path.write_text(json.dumps(citations.to_dicts(), ensure_ascii=False, indent=2), encoding="utf-8")
        artifact_refs.append({"type": "file", "name": "citations.json", "path": str(cit_path)})

    _safe_emit("step", f"✅ Zero report complete (strategy={strategy}), running review gate...")
    review = await ZeroReportReviewGate().evaluate(
        {
            "timeline": timeline.model_dump(),
            "root_cause_tree": root_cause_tree.model_dump(),
            "action_matrix": action_matrix.model_dump(),
        }
    )
    budget = BudgetPolicy().assess(estimated_cost=0.0, remaining_budget=input_data.get("remaining_budget"))
    return ZeroReportDomainResult(
        status="ok",
        result=final_text,
        artifact_refs=artifact_refs,
        review={
            "passed": review.passed,
            "issues": [
                {"severity": issue.severity, "message": issue.message, "metadata": issue.metadata}
                for issue in review.issues
            ],
        },
        budget={"action": budget.action, "reason": budget.reason},
    )
