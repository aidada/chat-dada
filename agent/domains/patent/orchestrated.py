"""
Patent domain agent — orchestrated version.

Uses the DomainOrchestrator for dynamic strategy composition.
Patent tasks are typically sequential (disclosure → prior-art → claims → spec → review),
but the orchestrator can switch to iterative refinement if the review gate fails.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field

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
from agent.domains.patent.prompts import (
    CLAIM_DRAFTER_PROMPT,
    DISCLOSURE_ANALYST_PROMPT,
    PATENT_DOMAIN_PROMPT,
    PATENT_REVIEWER_PROMPT,
    PRIOR_ART_RESEARCHER_PROMPT,
    SPECIFICATION_DRAFTER_PROMPT,
)
from agent.domains.patent.reviewers import PatentReviewGate
from agent.domains.patent.tools import get_patent_tools
from agent.platform.streaming import stream_nested_graph

from agent.workflows.orchestrator import build_orchestrated_graph, DomainSpec

_log = logging.getLogger("chatdada.patent.orchestrated")


# ── SubagentConfig (defined locally, PRD §8.3 C3) ───────────────────────────────


@dataclass
class SubagentConfig:
    """Configuration for a deepagents subagent."""

    name: str
    description: str
    system_prompt: str
    tools: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "tools": self.tools,
        }


# ── DomainSpec declaration ───────────────────────────────────────────────────

PATENT_SPEC = DomainSpec(
    name="patent",
    model_role="patent_domain",
    system_prompt=PATENT_DOMAIN_PROMPT,
    tools=get_patent_tools(),
    subagents=[
        SubagentConfig(
            name="technical_disclosure_analyst",
            description="Extract structured technical disclosure from user input.",
            system_prompt=DISCLOSURE_ANALYST_PROMPT,
            tools=get_patent_tools(),
        ),
        SubagentConfig(
            name="prior_art_researcher",
            description="Search for prior art and map coverage against claims.",
            system_prompt=PRIOR_ART_RESEARCHER_PROMPT,
            tools=get_patent_tools(),
        ),
        SubagentConfig(
            name="claim_drafter",
            description="Draft a patent claim tree with independent and dependent claims.",
            system_prompt=CLAIM_DRAFTER_PROMPT,
            tools=get_patent_tools(),
        ),
        SubagentConfig(
            name="specification_drafter",
            description="Draft the patent specification document.",
            system_prompt=SPECIFICATION_DRAFTER_PROMPT,
            tools=get_patent_tools(),
        ),
        SubagentConfig(
            name="patent_reviewer",
            description="Review the full patent draft for structural and semantic issues.",
            system_prompt=PATENT_REVIEWER_PROMPT,
            tools=get_patent_tools(),
        ),
    ],
    evaluator=PatentReviewGate(),
    strategy_hints=["sequential", "iterative"],  # patent is linear, with iterative refinement
    max_steps=6,
    max_cost=3.0,
)


# ── Compiled graph ───────────────────────────────────────────────────────────

_graph = build_orchestrated_graph(PATENT_SPEC)


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
    """Run patent domain using the dynamic workflow orchestrator."""
    query = str(
        input_data.get("query", input_data.get("task", "")) or ""
    ).strip()
    task_id = str(input_data.get("task_id", "") or "patent_preview")

    _log.info("Starting orchestrated patent: query=%s task_id=%s", query[:60], task_id)

    result = await stream_nested_graph(
        _graph,
        {
            "goal": query,
            "task_id": task_id,
            "report_profile": "",
            "cost": 0.0,
            "progress": 0.0,
            "confidence": 0.0,
            "max_cost": PATENT_SPEC.max_cost,
            "max_steps": PATENT_SPEC.max_steps,
            "intermediate_results": [],
            "evaluations": [],
            "step_history": [],
            "coverage": {},
        },
        config={"configurable": {"thread_id": task_id}},
        extra_payload={
            "nested_graph": "patent_orchestrated_graph",
            "domain_name": "patent",
            "source": "domain_orchestrated_wrapper",
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
