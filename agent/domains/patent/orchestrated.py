"""
Patent domain agent — orchestrated version.

Uses domain-internal workflow for patent tasks.
(PRD §8.3 C1: inlined build_orchestrated_graph to domain module)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agent.capabilities.citation_manager import CitationMap
from agent.capabilities.evidence_store import EvidenceCollection, EvidenceItem
from agent.domains.patent.agent import (
    PATENT_DATA_ROOT,
    PatentDomainResult,
    _build_claim_tree,
    _build_disclosure,
    _build_prior_art,
    _build_spec,
    _persist_artifacts,
)
from agent.domains.patent.workflow import (
    build_patent_workflow_graph,
    PATENT_MAX_COST,
    PATENT_MAX_STEPS,
)
from agent.platform.streaming import stream_nested_graph

_log = logging.getLogger("chatdada.patent.orchestrated")


# ── Compiled graph ───────────────────────────────────────────────────────────

_graph = build_patent_workflow_graph()


def _persist_orchestrated_artifacts(task_id: str, query: str, report: str) -> list[dict[str, Any]]:
    disclosure = _build_disclosure(query)
    claim_tree = _build_claim_tree(disclosure)
    prior_art_items, matrix = _build_prior_art(disclosure, claim_tree)
    spec_draft = _build_spec(disclosure, claim_tree)

    refs = _persist_artifacts(
        task_id=task_id,
        disclosure=disclosure,
        prior_art_items=prior_art_items,
        claim_tree=claim_tree,
        matrix=matrix,
        spec_draft=spec_draft,
        report=report,
    )

    evidence = EvidenceCollection(task_id=task_id)
    citations = CitationMap()
    for pa in prior_art_items:
        evidence.add(
            EvidenceItem(
                evidence_id=f"ev_{len(evidence.items) + 1}",
                evidence_type="url" if pa.source.startswith("http") else "quote",
                source=pa.source or pa.title,
                summary=pa.summary,
                metadata={"relation_to_claims": pa.relation_to_claims},
            )
        )
        if pa.source.startswith("http"):
            citations.add(pa.source, title=pa.title)

    task_dir = Path(PATENT_DATA_ROOT) / task_id
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

async def run_patent_domain_orchestrated(
    input_data: dict[str, Any],
) -> PatentDomainResult:
    """Run patent domain using the domain-internal workflow."""
    query = str(
        input_data.get("query", input_data.get("task", "")) or ""
    ).strip()
    task_id = str(input_data.get("task_id", "") or "patent_preview")

    _log.info("Starting patent workflow: query=%s task_id=%s", query[:60], task_id)

    result = await stream_nested_graph(
        _graph,
        {
            "goal": query,
            "task_id": task_id,
            "report_profile": "",
            "cost": 0.0,
            "progress": 0.0,
            "confidence": 0.0,
            "max_cost": PATENT_MAX_COST,
            "max_steps": PATENT_MAX_STEPS,
            "intermediate_results": [],
            "evaluations": [],
            "step_history": [],
            "coverage": {},
        },
        config={"configurable": {"thread_id": task_id}},
        extra_payload={
            "nested_graph": "patent_workflow",
            "domain_name": "patent",
            "source": "patent_workflow",
        },
    )

    final_text = result.get("final_result", "")
    strategy_trace = result.get("step_history", [])
    evals = result.get("evaluations", [])
    last_eval = evals[-1] if evals else {}
    artifact_refs = _persist_orchestrated_artifacts(task_id, query, final_text) if final_text else []

    strategies_used = [s.get("strategy", "") for s in strategy_trace]

    return PatentDomainResult(
        status="ok",
        result=final_text or "专利草案未能生成。",
        artifact_refs=artifact_refs,
        review={
            "passed": last_eval.get("passed", False),
            "issues": last_eval.get("issues", []),
        },
        budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
    )
