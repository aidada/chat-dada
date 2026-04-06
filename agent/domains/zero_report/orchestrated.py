"""
Zero-report domain agent — orchestrated version.

Uses domain-internal workflow for zero-report tasks.
(PRD §8.3 C1: inlined build_orchestrated_graph to domain module)

Zero-report tasks typically need planning (decompose into timeline → root-cause → actions → draft),
then iterative refinement if review fails.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agent.capabilities.citation_manager import CitationMap
from agent.capabilities.evidence_store import EvidenceCollection, EvidenceItem
from agent.domains.zero_report.agent import (
    ZERO_REPORT_DATA_ROOT,
    ZeroReportDomainResult,
    _build_actions,
    _build_draft,
    _build_facts,
    _build_root_cause,
    _build_timeline,
    _persist_artifacts,
)
from agent.domains.zero_report.workflow import (
    build_zero_report_workflow_graph,
    ZERO_REPORT_MAX_COST,
    ZERO_REPORT_MAX_STEPS,
)
from agent.platform.streaming import stream_nested_graph

_log = logging.getLogger("chatdada.zero_report.orchestrated")


# ── Compiled graph ───────────────────────────────────────────────────────────

_graph = build_zero_report_workflow_graph()


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
    """Run zero-report domain using the domain-internal workflow."""
    query = str(
        input_data.get("query", input_data.get("task", "")) or ""
    ).strip()
    task_id = str(input_data.get("task_id", "") or "zero_report_preview")

    _log.info("Starting zero-report workflow: query=%s task_id=%s", query[:60], task_id)

    result = await stream_nested_graph(
        _graph,
        {
            "goal": query,
            "task_id": task_id,
            "report_profile": "",
            "cost": 0.0,
            "progress": 0.0,
            "confidence": 0.0,
            "max_cost": ZERO_REPORT_MAX_COST,
            "max_steps": ZERO_REPORT_MAX_STEPS,
            "intermediate_results": [],
            "evaluations": [],
            "step_history": [],
            "coverage": {},
        },
        config={"configurable": {"thread_id": task_id}},
        extra_payload={
            "nested_graph": "zero_report_workflow",
            "domain_name": "zero_report",
            "source": "zero_report_workflow",
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
        budget={"action": "allow", "reason": f"workflow({' → '.join(strategies_used)})"},
    )
