"""
Zero-report domain agent — orchestrated version.

Uses the DomainOrchestrator for dynamic strategy composition.
Zero-report tasks typically need planning (decompose into timeline → root-cause → actions → draft),
then sequential or parallel execution, with iterative refinement if review fails.
"""
from __future__ import annotations

import logging
from typing import Any

from domain_agents.zero_report.agent import ZeroReportDomainResult
from domain_agents.zero_report.prompts import (
    ACTION_PLANNER_PROMPT,
    BASE_ZERO_REPORT_SYSTEM,
    ROOT_CAUSE_ANALYST_PROMPT,
    TIMELINE_BUILDER_PROMPT,
)
from domain_agents.zero_report.reviewers import ZeroReportReviewGate
from domain_agents.zero_report.tools import get_zero_report_tools

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

    result = await _graph.ainvoke({
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
    })

    final_text = result.get("final_result", "")
    strategy_trace = result.get("step_history", [])
    evals = result.get("evaluations", [])
    last_eval = evals[-1] if evals else {}

    strategies_used = [s.get("strategy", "") for s in strategy_trace]

    return ZeroReportDomainResult(
        status="ok",
        result=final_text or "归零报告未能生成。",
        artifact_refs=[],
        review={
            "passed": last_eval.get("passed", False),
            "issues": last_eval.get("issues", []),
        },
        budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
    )
