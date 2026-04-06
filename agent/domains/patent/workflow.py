"""
Patent domain internal workflow.

Provides domain-specific LangGraph workflow for patent tasks.
Uses sequential + iterative strategies as defined in PRD §8.3 C1.

This module inlines the necessary orchestrator logic to remove
dependency on agent.workflows.orchestrator.
"""
from __future__ import annotations

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
from agent.capabilities.review_gates import ReviewGate
from agent.domains.patent.reviewers import PatentReviewGate
from agent.domains.patent.tools import get_patent_tools
from agent.platform.streaming import stream_nested_graph

_log = logging.getLogger("chatdada.patent.workflow")

# ── Domain Configuration (PRD §8.3 C3/C1) ──────────────────────────────────────

PATENT_MODEL_ROLE = "patent_domain"
PATENT_MAX_STEPS = 6
PATENT_MAX_COST = 3.0

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


# ── Patent subagents ───────────────────────────────────────────────────────────

PATENT_SUBAGENTS = [
    SubagentConfig(
        name="technical_disclosure_analyst",
        description="Extract structured technical disclosure from user input.",
        system_prompt="请从技术描述中提取结构化的技术交底书，包括技术问题、解决方案、创新点。输出 JSON。",
        tools=get_patent_tools(),
    ),
    SubagentConfig(
        name="prior_art_researcher",
        description="Search for prior art and map coverage against claims.",
        system_prompt="搜索现有技术并对比权利要求覆盖范围。输出对比矩阵。",
        tools=get_patent_tools(),
    ),
    SubagentConfig(
        name="claim_drafter",
        description="Draft a patent claim tree with independent and dependent claims.",
        system_prompt="撰写专利权利要求树，包括独立权利要求和从属权利要求。",
        tools=get_patent_tools(),
    ),
    SubagentConfig(
        name="specification_drafter",
        description="Draft the patent specification document.",
        system_prompt="撰写专利说明书，包括背景技术、发明内容、具体实施方式。",
        tools=get_patent_tools(),
    ),
    SubagentConfig(
        name="patent_reviewer",
        description="Review the full patent draft for structural and semantic issues.",
        system_prompt="审查专利草案的结构和语义问题。输出 JSON 数组，每条含 severity 和 message。",
        tools=get_patent_tools(),
    ),
]

# ── Workflow State ─────────────────────────────────────────────────────────────

class PatentWorkflowState(TypedDict, total=False):
    """State for patent domain workflow."""
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

def _safe_emit(event_type: str, content: str | dict[str, Any]) -> None:
    try:
        from langgraph.config import get_stream_writer
        payload = dict(content) if isinstance(content, dict) else {"content": content}
        payload.setdefault("event_type", event_type)
        get_stream_writer()(payload)
    except Exception:
        pass


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
    return [s.to_dict() for s in PATENT_SUBAGENTS]


# ── Strategy nodes ─────────────────────────────────────────────────────────────

async def exec_sequential(state: PatentWorkflowState) -> dict[str, Any]:
    """Sequential strategy: single deepagents instance."""
    _safe_emit("step", "Patent: Sequential execution...")

    # Build context from prior results
    context_parts = [
        r["output"]
        for r in state.get("intermediate_results", [])
        if r.get("output")
    ]
    context = "\n\n---\n\n".join(context_parts[-3:]) if context_parts else ""

    from agent.domains.patent.prompts import PATENT_DOMAIN_PROMPT
    agent = create_deep_agent(
        model=build_chat_model(PATENT_MODEL_ROLE),
        system_prompt=PATENT_DOMAIN_PROMPT,
        tools=get_patent_tools(),
        subagents=_build_subagent_dicts(),
        checkpointer=False,
        name="patent_sequential",
    )

    input_msg = (
        f"{state['goal']}\n\n已有上下文：\n{context}"
        if context
        else state["goal"]
    )
    response = await stream_nested_graph(
        agent,
        {"messages": [HumanMessage(content=input_msg)]},
        extra_payload={
            "nested_graph": "patent_sequential",
            "strategy": "sequential",
            "source": "patent_workflow",
        },
    )
    output = _extract_last_ai_text(response)

    _safe_emit("step", f"Patent: Sequential done ({len(output)} chars)")
    return {
        "intermediate_results": [{"strategy": "sequential", "output": output}],
    }


async def exec_iterative(state: PatentWorkflowState) -> dict[str, Any]:
    """Iterative strategy: refine based on evaluation feedback."""
    _safe_emit("step", "Patent: Iterative refinement...")

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

    from agent.domains.patent.prompts import PATENT_DOMAIN_PROMPT
    agent = create_deep_agent(
        model=build_chat_model(PATENT_MODEL_ROLE),
        system_prompt=PATENT_DOMAIN_PROMPT,
        tools=get_patent_tools(),
        subagents=_build_subagent_dicts(),
        checkpointer=False,
        name="patent_iterative",
    )
    response = await stream_nested_graph(
        agent,
        {"messages": [HumanMessage(content=input_msg)]},
        extra_payload={
            "nested_graph": "patent_iterative",
            "strategy": "iterative",
            "source": "patent_workflow",
        },
    )
    output = _extract_last_ai_text(response)

    iteration = len(
        [e for e in state.get("evaluations", []) if not e.get("passed")]
    )
    _safe_emit("step", f"Patent: Iterative round {iteration + 1} done")
    return {
        "intermediate_results": [
            {"strategy": "iterative", "output": output, "iteration": iteration},
        ],
    }


# ── Analyze node ───────────────────────────────────────────────────────────────

async def analyze_node(state: PatentWorkflowState) -> dict[str, Any]:
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

async def select_strategy_node(state: PatentWorkflowState) -> dict[str, Any]:
    """Select execution strategy.

    Patent domain prefers: sequential (default) or iterative.
    Strategy is determined:
    1. If `selected_strategy` is in state (from Coordinator), use it
    2. If last evaluation failed, use iterative
    3. Default to sequential
    """
    if state.get("selected_strategy"):
        strategy = state["selected_strategy"]
    else:
        evals = state.get("evaluations", [])
        if evals and not evals[-1].get("passed"):
            strategy = "iterative"
        else:
            strategy = "sequential"

    valid_strategies = {"sequential", "iterative"}
    if strategy not in valid_strategies:
        strategy = "sequential"

    _log.info("Patent strategy selected: %s", strategy)

    try:
        from langgraph.config import get_stream_writer
        get_stream_writer()(
            {
                "event_type": "strategy",
                "strategy": strategy,
                "content": f"Strategy selected: {strategy}",
            }
        )
    except Exception:
        pass

    return {
        "selected_strategy": strategy,
        "step_history": [{
            "strategy": strategy,
            "confidence": 1.0,
            "reasoning": f"Strategy for patent domain",
        }],
    }


# ── Evaluator node ─────────────────────────────────────────────────────────────

async def evaluate_node(state: PatentWorkflowState) -> dict[str, Any]:
    """Evaluate using PatentReviewGate."""
    results = state.get("intermediate_results", [])
    if not results:
        return {
            "evaluations": [
                {"passed": False, "confidence": 0.0, "issues": []},
            ],
        }

    last = results[-1]
    output = last.get("output", "")

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
    evaluator = PatentReviewGate()
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
        _safe_emit("step", "Patent review passed")
        _safe_emit(
            "review",
            {
                "status": "passed",
                "issues": [],
                "content": "Review passed",
            },
        )
        return {
            "evaluations": [evaluation],
            "final_result": output,
            "confidence": 0.9,
        }

    issue_count = len(review.issues)
    _safe_emit(
        "step",
        f"Patent review failed ({issue_count} issues), preparing iterative refinement",
    )
    _safe_emit(
        "review",
        {
            "status": "failed",
            "issues": evaluation["issues"],
            "content": f"Review failed with {issue_count} issue(s)",
        },
    )
    return {
        "evaluations": [evaluation],
        "confidence": 0.4,
    }


# ── Control flow ───────────────────────────────────────────────────────────────

def route_to_strategy(state: PatentWorkflowState) -> str:
    return f"exec_{state['selected_strategy']}"


def should_continue(state: PatentWorkflowState) -> str:
    if state.get("final_result"):
        return "done"

    max_cost = state.get("max_cost", PATENT_MAX_COST)
    if state.get("cost", 0) >= max_cost:
        _log.warning("Patent cost limit reached: $%.2f", state["cost"])
        return "done"

    max_steps = state.get("max_steps", PATENT_MAX_STEPS)
    if len(state.get("step_history", [])) >= max_steps:
        _log.warning("Patent step limit reached: %d", len(state["step_history"]))
        return "done"

    return "continue"


# ── Graph builder ──────────────────────────────────────────────────────────────

def build_patent_workflow_graph() -> Any:
    """Build patent domain internal LangGraph.

    Workflow:
        START → analyze → select_strategy → exec_{strategy} → evaluate
                  ↑                                              │
                  └──────────── continue ────────────────────────┘
                                                                 │
                                                              done → END
    """
    graph = StateGraph(PatentWorkflowState)

    # Nodes
    graph.add_node("analyze", analyze_node)
    graph.add_node("select_strategy", select_strategy_node)
    graph.add_node("exec_sequential", exec_sequential)
    graph.add_node("exec_iterative", exec_iterative)
    graph.add_node("evaluate", evaluate_node)

    # Edges
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "select_strategy")
    graph.add_conditional_edges(
        "select_strategy",
        route_to_strategy,
        {
            "exec_sequential": "exec_sequential",
            "exec_iterative": "exec_iterative",
        },
    )
    for node in ("exec_sequential", "exec_iterative"):
        graph.add_edge(node, "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        should_continue,
        {"continue": "analyze", "done": END},
    )

    return graph.compile(name="patent_workflow")