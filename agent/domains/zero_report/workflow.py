"""
Zero-report domain internal workflow.

Provides domain-specific LangGraph workflow for zero-report tasks.
Uses planning + iterative strategies as defined in PRD §8.3 C1.

This module inlines the necessary orchestrator logic to remove
dependency on agent.workflows.orchestrator.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
from dataclasses import dataclass, field
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from typing_extensions import TypedDict

from core.content_utils import extract_result_text
from core.models import build_chat_model
from deepagents import create_deep_agent
from agent.domains.zero_report.reviewers import ZeroReportReviewGate
from agent.domains.zero_report.tools import get_zero_report_tools
from agent.platform.streaming import stream_nested_graph

_log = logging.getLogger("chatdada.zero_report.workflow")

# ── Domain Configuration (PRD §8.3 C3/C1) ──────────────────────────────────────

ZERO_REPORT_MODEL_ROLE = "zero_report_domain"
ZERO_REPORT_MAX_STEPS = 8
ZERO_REPORT_MAX_COST = 3.0

# ── SubagentConfig (local definition, PRD §8.3 C3) ──────────────────────────────

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


# ── Zero-report subagents ─────────────────────────────────────────────────────

from agent.domains.zero_report.prompts import (
    ACTION_PLANNER_PROMPT,
    BASE_ZERO_REPORT_SYSTEM,
    ROOT_CAUSE_ANALYST_PROMPT,
    TIMELINE_BUILDER_PROMPT,
)

ZERO_REPORT_SUBAGENTS = [
    SubagentConfig(
        name="incident_structurer",
        description="Extract structured incident facts from raw event description.",
        system_prompt="请从事件描述中提取结构化的事件摘要，包括标题、摘要、影响范围。输出 JSON。",
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
]

# ── Workflow State ─────────────────────────────────────────────────────────────

class ZeroReportWorkflowState(TypedDict, total=False):
    """State for zero-report domain workflow."""
    # Input
    goal: str
    task_id: str
    report_profile: str

    # Strategy control
    selected_strategy: str
    step_history: Annotated[list[dict[str, Any]], "add"]

    # Progress signals
    progress: float
    confidence: float
    coverage: dict[str, bool]
    cost: float
    max_cost: float
    max_steps: int

    # Results
    intermediate_results: Annotated[list[dict[str, Any]], "add"]
    evaluations: Annotated[list[dict[str, Any]], "add"]
    final_result: str


# ── Helper functions ───────────────────────────────────────────────────────────

from agent.platform.emit import safe_emit_progress_with_content as _safe_emit


def _extract_last_ai_text(response: Any) -> str:
    """Extract text from the last AIMessage in a deepagents response."""
    messages = response.get("messages", []) if isinstance(response, dict) else []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = extract_result_text(getattr(msg, "content", ""))
            if text:
                return text
    return ""


def _build_subagent_dicts() -> list[dict[str, Any]]:
    return [s.to_dict() for s in ZERO_REPORT_SUBAGENTS]


# ── Strategy nodes ─────────────────────────────────────────────────────────────

async def exec_planning(state: ZeroReportWorkflowState) -> dict[str, Any]:
    """Planning strategy: decompose goal into subtask coverage map."""
    _safe_emit("step", "Zero-report: Planning task decomposition...")

    agent = create_deep_agent(
        model=build_chat_model(ZERO_REPORT_MODEL_ROLE),
        system_prompt=(
            "你是任务规划专家。分析目标后输出 JSON 格式的子任务计划。\n"
            '{"subtasks": [{"id": "sub_1", "topic": "子任务描述"}, ...]}\n'
            "子任务应尽量独立，便于并行执行。通常 2-5 个子任务。\n"
            "只输出 JSON，不要其他内容。"
        ),
        tools=[],
        checkpointer=False,
        name="zero_report_planner",
    )
    response = await stream_nested_graph(
        agent,
        {"messages": [HumanMessage(
            content=f"请为以下目标制定子任务计划：\n{state['goal']}",
        )]},
        extra_payload={
            "nested_graph": "zero_report_planner",
            "strategy": "planning",
            "source": "zero_report_workflow",
        },
    )
    plan_text = _extract_last_ai_text(response)

    # Parse plan -> coverage map
    try:
        cleaned = plan_text
        if "```" in cleaned:
            cleaned = (
                cleaned.split("```json")[-1].split("```")[0]
                if "```json" in cleaned
                else cleaned.split("```")[1].split("```")[0]
            )
        plan = _json.loads(cleaned.strip())
        subtasks = plan.get("subtasks", [])
        coverage = {st["id"]: False for st in subtasks}
    except (ValueError, KeyError):
        _log.warning("Plan parsing failed, creating single-subtask fallback")
        coverage = {"sub_1": False}
        subtasks = [{"id": "sub_1", "topic": state["goal"]}]

    _safe_emit("step", f"Zero-report: Planned {len(subtasks)} subtasks")
    _safe_emit(
        "plan",
        {
            "status": "generated",
            "subtasks": _json.loads(_json.dumps(subtasks)),
            "content": f"Plan generated: {len(subtasks)} subtasks",
        },
    )
    return {
        "intermediate_results": [
            {"strategy": "planning", "plan": subtasks},
        ],
        "coverage": coverage,
    }


async def exec_iterative(state: ZeroReportWorkflowState) -> dict[str, Any]:
    """Iterative strategy: refine based on evaluation feedback."""
    _safe_emit("step", "Zero-report: Iterative refinement...")

    last_output = ""
    if state.get("intermediate_results"):
        last_output = state["intermediate_results"][-1].get("output", "")

    feedback = ""
    if state.get("evaluations"):
        issues = state["evaluations"][-1].get("issues", [])
        if issues:
            feedback = "\n".join(
                f"- [{i['severity']}] {i['message']}" for i in issues
            )

    if last_output and feedback:
        input_msg = (
            f"目标：{state['goal']}\n\n"
            f"上一版本：\n{last_output}\n\n"
            f"评审反馈（请逐条改进）：\n{feedback}"
        )
    else:
        input_msg = state["goal"]

    agent = create_deep_agent(
        model=build_chat_model(ZERO_REPORT_MODEL_ROLE),
        system_prompt=BASE_ZERO_REPORT_SYSTEM,
        tools=get_zero_report_tools(),
        subagents=_build_subagent_dicts(),
        checkpointer=False,
        name="zero_report_iterative",
    )
    response = await stream_nested_graph(
        agent,
        {"messages": [HumanMessage(content=input_msg)]},
        extra_payload={
            "nested_graph": "zero_report_iterative",
            "strategy": "iterative",
            "source": "zero_report_workflow",
        },
    )
    output = _extract_last_ai_text(response)

    iteration = len(
        [e for e in state.get("evaluations", []) if not e.get("passed")]
    )
    _safe_emit("step", f"Zero-report: Iterative round {iteration + 1} done")
    return {
        "intermediate_results": [
            {"strategy": "iterative", "output": output, "iteration": iteration},
        ],
    }


# ── Analyze node ───────────────────────────────────────────────────────────────

async def analyze_node(state: ZeroReportWorkflowState) -> dict[str, Any]:
    """Compute progress signals from current state."""
    coverage = state.get("coverage", {})
    if coverage:
        progress = sum(v for v in coverage.values()) / len(coverage)
    else:
        progress = 0.0

    evals = state.get("evaluations", [])
    confidence = evals[-1].get("confidence", 0.0) if evals else 0.0

    return {
        "progress": progress,
        "confidence": confidence,
    }


# ── Strategy selection ─────────────────────────────────────────────────────────

async def select_strategy_node(state: ZeroReportWorkflowState) -> dict[str, Any]:
    """Select execution strategy.

    Zero-report domain prefers: planning (first) then iterative.
    Strategy is determined:
    1. If `selected_strategy` is in state (from Coordinator), use it
    2. If no intermediate results, use planning
    3. If last evaluation failed, use iterative
    4. Default to planning
    """
    if state.get("selected_strategy"):
        strategy = state["selected_strategy"]
    elif not state.get("intermediate_results"):
        strategy = "planning"
    elif state.get("evaluations") and not state["evaluations"][-1].get("passed"):
        strategy = "iterative"
    else:
        strategy = "planning"

    valid_strategies = {"planning", "iterative"}
    if strategy not in valid_strategies:
        strategy = "planning"

    _log.info("Zero-report strategy selected: %s", strategy)

    from agent.platform.emit import safe_emit_progress

    safe_emit_progress(
        "progress.brief",
        {
            "strategy": strategy,
            "text": f"Strategy selected: {strategy}",
            "content": f"Strategy selected: {strategy}",
        },
    )

    return {
        "selected_strategy": strategy,
        "step_history": [{
            "strategy": strategy,
            "confidence": 1.0,
            "reasoning": f"Strategy for zero-report domain",
        }],
    }


# ── Evaluator node ─────────────────────────────────────────────────────────────

async def evaluate_node(state: ZeroReportWorkflowState) -> dict[str, Any]:
    """Evaluate using ZeroReportReviewGate."""
    results = state.get("intermediate_results", [])
    if not results:
        return {
            "evaluations": [
                {"passed": False, "confidence": 0.0, "issues": []},
            ],
        }

    last = results[-1]
    output = last.get("output", "")

    # Planning strategy doesn't produce evaluable output
    if last.get("strategy") == "planning":
        return {
            "evaluations": [
                {
                    "passed": True,
                    "confidence": 0.7,
                    "issues": [],
                    "note": "plan generated, not final output",
                },
            ],
        }

    if not output:
        return {
            "evaluations": [
                {
                    "passed": False,
                    "confidence": 0.0,
                    "issues": [
                        {"severity": "error", "message": "策略未产出任何输出"},
                    ],
                },
            ],
        }

    # Run domain-specific ReviewGate
    evaluator = ZeroReportReviewGate()
    review = await evaluator.evaluate({"report": output})

    evaluation: dict[str, Any] = {
        "passed": review.passed,
        "confidence": 0.9 if review.passed else 0.4,
        "issues": [
            {
                "severity": i.severity,
                "message": i.message,
                "metadata": i.metadata,
            }
            for i in review.issues
        ],
    }

    if review.passed:
        _safe_emit("step", "Zero-report review passed")
        return {
            "evaluations": [evaluation],
            "final_result": output,
            "confidence": 0.9,
        }

    issue_count = len(review.issues)
    _safe_emit(
        "step",
        f"Zero-report review failed ({issue_count} issues), preparing iterative refinement",
    )
    return {
        "evaluations": [evaluation],
        "confidence": 0.4,
    }


# ── Control flow ───────────────────────────────────────────────────────────────

def route_to_strategy(state: ZeroReportWorkflowState) -> str:
    return f"exec_{state['selected_strategy']}"


def should_continue(state: ZeroReportWorkflowState) -> str:
    if state.get("final_result"):
        return "done"

    max_cost = state.get("max_cost", ZERO_REPORT_MAX_COST)
    if state.get("cost", 0) >= max_cost:
        _log.warning("Zero-report cost limit reached: $%.2f", state["cost"])
        return "done"

    max_steps = state.get("max_steps", ZERO_REPORT_MAX_STEPS)
    if len(state.get("step_history", [])) >= max_steps:
        _log.warning("Zero-report step limit reached: %d", len(state["step_history"]))
        return "done"

    return "continue"


# ── Graph builder ──────────────────────────────────────────────────────────────

def build_zero_report_workflow_graph() -> Any:
    """Build zero-report domain internal LangGraph.

    Workflow:
        START → analyze → select_strategy → exec_{strategy} → evaluate
                  ↑                                              │
                  └──────────── continue ────────────────────────┘
                                                                 │
                                                              done → END
    """
    graph = StateGraph(ZeroReportWorkflowState)

    # Nodes
    graph.add_node("analyze", analyze_node)
    graph.add_node("select_strategy", select_strategy_node)
    graph.add_node("exec_planning", exec_planning)
    graph.add_node("exec_iterative", exec_iterative)
    graph.add_node("evaluate", evaluate_node)

    # Edges
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "select_strategy")
    graph.add_conditional_edges(
        "select_strategy",
        route_to_strategy,
        {
            "exec_planning": "exec_planning",
            "exec_iterative": "exec_iterative",
        },
    )
    for node in ("exec_planning", "exec_iterative"):
        graph.add_edge(node, "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        should_continue,
        {"continue": "analyze", "done": END},
    )

    return graph.compile(name="zero_report_workflow")
