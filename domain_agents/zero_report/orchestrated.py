"""
Zero-report domain agent — orchestrated version.

Uses the DomainOrchestrator for dynamic strategy composition.
Zero-report tasks typically need planning (decompose into timeline → root-cause → actions → draft),
then sequential or parallel execution, with iterative refinement if review fails.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from capabilities.citation_manager import CitationMap
from capabilities.evidence_store import EvidenceCollection, EvidenceItem
from domain_agents.zero_report.agent import (
    ZERO_REPORT_DATA_ROOT,
    ZeroReportDomainResult,
    _build_actions,
    _build_draft,
    _build_facts,
    _build_root_cause,
    _build_timeline,
    _persist_artifacts,
)
from domain_agents.zero_report.prompts import (
    ACTION_PLANNER_PROMPT,
    BASE_ZERO_REPORT_SYSTEM,
    ROOT_CAUSE_ANALYST_PROMPT,
    TIMELINE_BUILDER_PROMPT,
)
from domain_agents.zero_report.reviewers import ZeroReportReviewGate
from domain_agents.zero_report.tools import get_zero_report_tools
from task_platform.streaming import stream_nested_graph

from workflows.orchestrator import build_orchestrated_graph
from workflows.spec import DomainSpec, SubagentConfig

_log = logging.getLogger("chatdada.zero_report.orchestrated")


# ── DomainSpec declaration ───────────────────────────────────────────────────

ZERO_REPORT_SPEC = DomainSpec(
    name="zero_report",
    model_role="zero_report_domain",
    system_prompt=BASE_ZERO_REPORT_SYSTEM,
    tools=get_zero_report_tools(),
    subagents=[
        SubagentConfig(
            name="incident_structurer",
            description="Extract structured incident facts from raw event description.",
            system_prompt=(
                "请从事件描述中提取结构化的事件摘要，包括标题、摘要、影响范围。输出 JSON。"
            ),
            tools=get_zero_report_tools(),
        ),
        SubagentConfig(
            name="timeline_builder",
            description="Build a chronological timeline of key events.",
            system_prompt=TIMELINE_BUILDER_PROMPT,
            tools=get_zero_report_tools(),
        ),
        SubagentConfig(
            name="root_cause_analyst",
            description="Perform root cause analysis using the '5 Whys' method.",
            system_prompt=ROOT_CAUSE_ANALYST_PROMPT,
            tools=get_zero_report_tools(),
        ),
        SubagentConfig(
            name="corrective_action_planner",
            description="Define corrective actions with owners and deadlines.",
            system_prompt=ACTION_PLANNER_PROMPT,
            tools=get_zero_report_tools(),
        ),
        SubagentConfig(
            name="report_reviewer",
            description="Review the complete zero report for completeness and accountability.",
            system_prompt=(
                "请审查归零报告的完整性：时间线是否完整、根因是否达到可行动层、"
                "整改措施是否有责任人和时限。输出 JSON 数组，每条含 severity 和 message。"
            ),
            tools=get_zero_report_tools(),
        ),
    ],
    evaluator=ZeroReportReviewGate(),
    strategy_hints=["planning", "iterative"],  # decompose first, then refine
    max_steps=8,
    max_cost=3.0,
)


# ── Compiled graph ───────────────────────────────────────────────────────────

_graph = build_orchestrated_graph(ZERO_REPORT_SPEC)


def _persist_orchestrated_artifacts(task_id: str, query: str, report: str) -> list[dict[str, Any]]:
    facts = _build_facts(query)
    timeline = _build_timeline(query)
    root_cause_tree = _build_root_cause(query)
    action_matrix = _build_actions()
    draft = _build_draft(facts)

    refs = _persist_artifacts(
        task_id=task_id,
        facts=facts,
        timeline=timeline,
        root_cause_tree=root_cause_tree,
        action_matrix=action_matrix,
        draft=draft,
        report=report,
    )

    evidence = EvidenceCollection(task_id=task_id)
    citations = CitationMap()
    for event in timeline.events:
        evidence.add(
            EvidenceItem(
                evidence_id=f"ev_{len(evidence.items) + 1}",
                evidence_type="quote",
                source=f"timeline:{event.timestamp}",
                summary=event.detail,
            )
        )
    for item in action_matrix.items:
        evidence.add(
            EvidenceItem(
                evidence_id=f"ev_{len(evidence.items) + 1}",
                evidence_type="quote",
                source=f"action:{item.owner}",
                summary=f"{item.action} (due: {item.due_date})",
            )
        )

    task_dir = Path(ZERO_REPORT_DATA_ROOT) / task_id
    if evidence.items:
        ev_path = task_dir / "evidence.json"
        ev_path.write_text(
            json.dumps(
                [
                    {
                        "evidence_id": e.evidence_id,
                        "type": e.evidence_type,
                        "source": e.source,
                        "summary": e.summary,
                    }
                    for e in evidence.items
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        refs.append({"type": "file", "name": "evidence.json", "path": str(ev_path)})
    if citations.all():
        cit_path = task_dir / "citations.json"
        cit_path.write_text(json.dumps(citations.to_dicts(), ensure_ascii=False, indent=2), encoding="utf-8")
        refs.append({"type": "file", "name": "citations.json", "path": str(cit_path)})

    return refs


# ── Entry point ──────────────────────────────────────────────────────────────

async def run_zero_report_domain_orchestrated(
    input_data: dict[str, Any],
) -> ZeroReportDomainResult:
    """Run zero-report domain using the dynamic workflow orchestrator."""
    query = str(
        input_data.get("query", input_data.get("task", "")) or ""
    ).strip()
    task_id = str(input_data.get("task_id", "") or "zero_report_preview")

    _log.info("Starting orchestrated zero-report: query=%s task_id=%s", query[:60], task_id)

    result = await stream_nested_graph(
        _graph,
        {
            "goal": query,
            "task_id": task_id,
            "report_profile": "",
            "cost": 0.0,
            "progress": 0.0,
            "confidence": 0.0,
            "max_cost": ZERO_REPORT_SPEC.max_cost,
            "max_steps": ZERO_REPORT_SPEC.max_steps,
            "intermediate_results": [],
            "evaluations": [],
            "step_history": [],
            "coverage": {},
        },
        config={"configurable": {"thread_id": task_id}},
        extra_payload={
            "nested_graph": "zero_report_orchestrated_graph",
            "domain_name": "zero_report",
            "source": "domain_orchestrated_wrapper",
        },
    )

    final_text = result.get("final_result", "")
    strategy_trace = result.get("step_history", [])
    evals = result.get("evaluations", [])
    last_eval = evals[-1] if evals else {}
    artifact_refs = _persist_orchestrated_artifacts(task_id, query, final_text) if final_text else []

    strategies_used = [s.get("strategy", "") for s in strategy_trace]

    return ZeroReportDomainResult(
        status="ok",
        result=final_text or "归零报告未能生成。",
        artifact_refs=artifact_refs,
        review={
            "passed": last_eval.get("passed", False),
            "issues": last_eval.get("issues", []),
        },
        budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
    )
