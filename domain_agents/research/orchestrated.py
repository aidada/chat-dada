"""
Research domain agent — orchestrated version.

Uses the DomainOrchestrator for dynamic strategy composition
(planning → parallel → iterative) instead of hardcoded execution paths.

The interface is identical to ``run_research_domain`` in ``agent.py`` —
returns a ``ResearchDomainResult`` — so it can be a drop-in replacement
in the domain registry.
"""
from __future__ import annotations

import logging
from typing import Any

from domain_agents.research.agent import ResearchDomainResult
from domain_agents.research.prompts import BASE_RESEARCH_SYSTEM
from domain_agents.research.reviewers import ResearchReviewGate
from domain_agents.research.tools import get_research_tools

from workflows.orchestrator import build_orchestrated_graph
from workflows.spec import DomainSpec, SubagentConfig

_log = logging.getLogger("chatdada.research.orchestrated")


# ── DomainSpec declaration ───────────────────────────────────────────────────

RESEARCH_SPEC = DomainSpec(
    name="research",
    model_role="research_domain",
    system_prompt=BASE_RESEARCH_SYSTEM,
    tools=get_research_tools(),
    subagents=[
        SubagentConfig(
            name="web_researcher",
            description="Collect evidence from the web for a single research angle.",
            system_prompt=(
                "Focus on collecting concise evidence with sources. "
                "Use web_search and brave_search for initial discovery, "
                "academic_search for papers, exa_deep_search only for filling critical gaps."
            ),
            tools=get_research_tools(),
        ),
        SubagentConfig(
            name="evidence_synthesizer",
            description="Synthesize findings into a concise research summary.",
            system_prompt=(
                "Synthesize evidence into a structured Chinese summary with citations. "
                "Preserve source URLs and evidence strength indicators."
            ),
            tools=get_research_tools(),
        ),
    ],
    evaluator=ResearchReviewGate(),
    strategy_hints=["planning"],  # research tasks typically need decomposition first
    max_steps=8,
    max_cost=3.0,
)


# ── Compiled graph (built once at import time) ───────────────────────────────

_graph = build_orchestrated_graph(RESEARCH_SPEC)


# ── Entry point (same interface as run_research_domain in agent.py) ──────────

async def run_research_domain_orchestrated(
    input_data: dict[str, Any],
) -> ResearchDomainResult:
    """Run research domain using the dynamic workflow orchestrator.

    Accepts the same ``input_data`` dict as ``run_research_domain``
    and returns the same ``ResearchDomainResult``.
    """
    query = str(
        input_data.get("query", input_data.get("task", "")) or ""
    ).strip()
    task_id = str(input_data.get("task_id", "") or "")
    report_profile = str(input_data.get("report_profile", "") or "")

    _log.info(
        "Starting orchestrated research: query=%s task_id=%s",
        query[:60],
        task_id,
    )

    result = await _graph.ainvoke({
        "goal": query,
        "task_id": task_id,
        "report_profile": report_profile,
        "cost": 0.0,
        "progress": 0.0,
        "confidence": 0.0,
        "max_cost": RESEARCH_SPEC.max_cost,
        "max_steps": RESEARCH_SPEC.max_steps,
        "intermediate_results": [],
        "evaluations": [],
        "step_history": [],
        "coverage": {},
    })

    final_text = result.get("final_result", "")
    strategy_trace = result.get("step_history", [])
    evals = result.get("evaluations", [])
    last_eval = evals[-1] if evals else {}

    # Build strategy summary string for compatibility
    strategies_used = [s.get("strategy", "") for s in strategy_trace]
    strategy_summary = f"orchestrated({' → '.join(strategies_used)})"

    return ResearchDomainResult(
        status="ok",
        result=final_text or "研究未能产出最终结果。",
        artifact_refs=[],
        review={
            "passed": last_eval.get("passed", False),
            "issues": last_eval.get("issues", []),
        },
        strategy=strategy_summary,
    )
