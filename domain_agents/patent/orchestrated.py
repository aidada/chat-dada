"""
Patent domain agent — orchestrated version.

Uses the DomainOrchestrator for dynamic strategy composition.
Patent tasks are typically sequential (disclosure → prior-art → claims → spec → review),
but the orchestrator can switch to iterative refinement if the review gate fails.
"""
from __future__ import annotations

import logging
from typing import Any

from domain_agents.patent.agent import PatentDomainResult
from domain_agents.patent.prompts import (
    CLAIM_DRAFTER_PROMPT,
    DISCLOSURE_ANALYST_PROMPT,
    PATENT_DOMAIN_PROMPT,
    PATENT_REVIEWER_PROMPT,
    PRIOR_ART_RESEARCHER_PROMPT,
    SPECIFICATION_DRAFTER_PROMPT,
)
from domain_agents.patent.reviewers import PatentReviewGate
from domain_agents.patent.tools import get_patent_tools

from workflows.orchestrator import build_orchestrated_graph
from workflows.spec import DomainSpec, SubagentConfig

_log = logging.getLogger("chatdada.patent.orchestrated")


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

    result = await _graph.ainvoke({
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
    })

    final_text = result.get("final_result", "")
    strategy_trace = result.get("step_history", [])
    evals = result.get("evaluations", [])
    last_eval = evals[-1] if evals else {}

    strategies_used = [s.get("strategy", "") for s in strategy_trace]

    return PatentDomainResult(
        status="ok",
        result=final_text or "专利草案未能生成。",
        artifact_refs=[],
        review={
            "passed": last_eval.get("passed", False),
            "issues": last_eval.get("issues", []),
        },
        budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
    )
