"""
DomainOrchestrator — builds a LangGraph graph from any DomainSpec.

The graph implements: ANALYZE → SELECT STRATEGY → EXECUTE → EVALUATE → (loop or done)
Each strategy node internally uses ``deepagents.create_deep_agent()`` as the agent harness.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import operator
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from pydantic import BaseModel
from typing_extensions import TypedDict

from core.content_utils import extract_result_text
from workflows.spec import DomainSpec
from workflows.strategy_selector import make_strategy_selector

_log = logging.getLogger("chatdada.orchestrator")


# ── Orchestrator state ───────────────────────────────────────────────────────

class OrchestratorState(TypedDict, total=False):
    # Input
    goal: str
    task_id: str
    report_profile: str

    # Strategy control
    selected_strategy: str
    step_history: Annotated[list[dict[str, Any]], operator.add]

    # Progress signals (drive strategy selection)
    progress: float
    confidence: float
    coverage: dict[str, bool]
    cost: float
    max_cost: float
    max_steps: int

    # Results
    intermediate_results: Annotated[list[dict[str, Any]], operator.add]
    evaluations: Annotated[list[dict[str, Any]], operator.add]
    final_result: str


# ── Result model ─────────────────────────────────────────────────────────────

class OrchestratedDomainResult(BaseModel):
    status: str
    result: str
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    strategy_trace: list[dict[str, Any]]


# ── Shared helpers ───────────────────────────────────────────────────────────

def _safe_emit(event_type: str, content: str) -> None:
    try:
        from langgraph.config import get_stream_writer
        get_stream_writer()({"event_type": event_type, "content": content})
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


def _build_subagent_dicts(spec: DomainSpec) -> list[dict[str, Any]]:
    return [s.to_dict() for s in spec.subagents]


# ── Strategy nodes ───────────────────────────────────────────────────────────

def make_sequential(spec: DomainSpec):
    """Sequential strategy: single deepagents instance with full tool+subagent access."""

    async def exec_sequential(state: OrchestratorState) -> dict[str, Any]:
        from deepagents import create_deep_agent
        from core.models import build_chat_model

        _safe_emit("step", "▶ Sequential: 执行中...")

        # Build context from prior results
        context_parts = [
            r["output"]
            for r in state.get("intermediate_results", [])
            if r.get("output")
        ]
        context = "\n\n---\n\n".join(context_parts[-3:]) if context_parts else ""

        agent = create_deep_agent(
            model=build_chat_model(spec.model_role),
            system_prompt=spec.system_prompt,
            tools=spec.tools,
            subagents=_build_subagent_dicts(spec),
            checkpointer=False,
            name=f"{spec.name}_sequential",
        )

        input_msg = (
            f"{state['goal']}\n\n已有上下文：\n{context}"
            if context
            else state["goal"]
        )
        response = await agent.ainvoke(
            {"messages": [HumanMessage(content=input_msg)]}
        )
        output = _extract_last_ai_text(response)

        _safe_emit("step", f"▶ Sequential: 完成 ({len(output)} 字)")
        return {
            "intermediate_results": [{"strategy": "sequential", "output": output}],
        }

    return exec_sequential


def make_parallel(spec: DomainSpec):
    """Parallel strategy: fan-out concurrent deepagents instances, one per subtask."""

    async def exec_parallel(state: OrchestratorState) -> dict[str, Any]:
        from deepagents import create_deep_agent
        from core.models import build_chat_model

        coverage = state.get("coverage", {})
        pending = [k for k, v in coverage.items() if not v]

        if not pending:
            return {}

        _safe_emit("step", f"⚡ Parallel: {len(pending)} 个子任务并行执行中...")

        async def run_one(subtask_id: str) -> dict[str, Any]:
            agent = create_deep_agent(
                model=build_chat_model(spec.model_role),
                system_prompt=f"{spec.system_prompt}\n\n聚焦子任务：{subtask_id}",
                tools=spec.tools,
                subagents=_build_subagent_dicts(spec),
                checkpointer=False,
                name=f"{spec.name}_worker_{subtask_id}",
            )
            try:
                resp = await agent.ainvoke(
                    {"messages": [HumanMessage(
                        content=f"{state['goal']}\n\n当前子任务：{subtask_id}",
                    )]}
                )
                return {
                    "subtask_id": subtask_id,
                    "status": "ok",
                    "output": _extract_last_ai_text(resp),
                }
            except Exception as exc:
                _log.warning("Parallel worker %s failed: %s", subtask_id, exc)
                return {
                    "subtask_id": subtask_id,
                    "status": "error",
                    "error": str(exc),
                }

        results = await asyncio.gather(*[run_one(sid) for sid in pending])

        # Update coverage
        new_coverage = dict(coverage)
        for r in results:
            if r["status"] == "ok":
                new_coverage[r["subtask_id"]] = True

        # Synthesize successful results
        ok_results = [r for r in results if r["status"] == "ok"]
        if ok_results:
            findings = "\n\n".join(
                f"## {r['subtask_id']}\n{r['output']}" for r in ok_results
            )
            synth_agent = create_deep_agent(
                model=build_chat_model(spec.model_role),
                system_prompt=(
                    "你是研究综合专家。将各子任务结果整合为连贯的完整报告，"
                    "保留关键证据和引用。"
                ),
                tools=[],
                checkpointer=False,
                name=f"{spec.name}_synthesizer",
            )
            resp = await synth_agent.ainvoke(
                {"messages": [HumanMessage(
                    content=f"目标：{state['goal']}\n\n各子任务结果：\n{findings}",
                )]}
            )
            synthesis = _extract_last_ai_text(resp)
        else:
            synthesis = "所有并行子任务均失败。"

        ok_count = len(ok_results)
        total_count = len(results)
        _safe_emit("step", f"⚡ Parallel: {ok_count}/{total_count} 成功")
        return {
            "intermediate_results": [
                {"strategy": "parallel", "output": synthesis, "workers": results},
            ],
            "coverage": new_coverage,
        }

    return exec_parallel


def make_iterative(spec: DomainSpec):
    """Iterative strategy: refine previous output based on evaluation feedback."""

    async def exec_iterative(state: OrchestratorState) -> dict[str, Any]:
        from deepagents import create_deep_agent
        from core.models import build_chat_model

        _safe_emit("step", "🔄 Iterative: 根据反馈优化中...")

        # Get last output + evaluation feedback
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
            model=build_chat_model(spec.model_role),
            system_prompt=spec.system_prompt,
            tools=spec.tools,
            subagents=_build_subagent_dicts(spec),
            checkpointer=False,
            name=f"{spec.name}_iterative",
        )
        response = await agent.ainvoke(
            {"messages": [HumanMessage(content=input_msg)]}
        )
        output = _extract_last_ai_text(response)

        iteration = len(
            [e for e in state.get("evaluations", []) if not e.get("passed")]
        )
        _safe_emit("step", f"🔄 Iterative: 第 {iteration + 1} 轮优化完成")
        return {
            "intermediate_results": [
                {"strategy": "iterative", "output": output, "iteration": iteration},
            ],
        }

    return exec_iterative


def make_planning(spec: DomainSpec):
    """Planning strategy: decompose goal into subtask coverage map."""

    async def exec_planning(state: OrchestratorState) -> dict[str, Any]:
        from deepagents import create_deep_agent
        from core.models import build_chat_model

        _safe_emit("step", "📋 Planning: 任务分解中...")

        agent = create_deep_agent(
            model=build_chat_model(spec.model_role),
            system_prompt=(
                "你是任务规划专家。分析目标后输出 JSON 格式的子任务计划。\n"
                '格式：{"subtasks": [{"id": "sub_1", "topic": "子任务描述"}, ...]}\n'
                "子任务应尽量独立，便于并行执行。通常 2-5 个子任务。\n"
                "只输出 JSON，不要其他内容。"
            ),
            tools=[],
            checkpointer=False,
            name=f"{spec.name}_planner",
        )
        response = await agent.ainvoke(
            {"messages": [HumanMessage(
                content=f"请为以下目标制定子任务计划：\n{state['goal']}",
            )]}
        )
        plan_text = _extract_last_ai_text(response)

        # Parse plan → coverage map
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

        _safe_emit("step", f"📋 Planning: 已分解为 {len(subtasks)} 个子任务")
        return {
            "intermediate_results": [
                {"strategy": "planning", "plan": subtasks},
            ],
            "coverage": coverage,
        }

    return exec_planning


# ── Analyze node ─────────────────────────────────────────────────────────────

async def analyze_node(state: OrchestratorState) -> dict[str, Any]:
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


# ── Evaluate node ────────────────────────────────────────────────────────────

def make_evaluator(spec: DomainSpec):
    """Build evaluate node using the domain's ReviewGate."""

    async def evaluate_node(state: OrchestratorState) -> dict[str, Any]:
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
        review = await spec.evaluator.evaluate({"report": output})

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
            _safe_emit("step", "✅ 评审通过")
            return {
                "evaluations": [evaluation],
                "final_result": output,
                "confidence": 0.9,
            }

        issue_count = len(review.issues)
        _safe_emit(
            "step",
            f"⚠️ 评审未通过 ({issue_count} 个问题)，准备迭代优化",
        )
        return {
            "evaluations": [evaluation],
            "confidence": 0.4,
        }

    return evaluate_node


# ── Control flow ─────────────────────────────────────────────────────────────

def route_to_strategy(state: OrchestratorState) -> str:
    return f"exec_{state['selected_strategy']}"


def should_continue(state: OrchestratorState) -> str:
    if state.get("final_result"):
        return "done"

    max_cost = state.get("max_cost", 5.0)
    if state.get("cost", 0) >= max_cost:
        _log.warning("Cost limit reached: $%.2f", state["cost"])
        return "done"

    max_steps = state.get("max_steps", 10)
    if len(state.get("step_history", [])) >= max_steps:
        _log.warning("Step limit reached: %d", len(state["step_history"]))
        return "done"

    return "continue"


# ── Graph builder ────────────────────────────────────────────────────────────

def build_orchestrated_graph(spec: DomainSpec):
    """Build a LangGraph graph that orchestrates any domain via DomainSpec.

    Returns a compiled ``StateGraph`` with dynamic strategy composition:

    .. code-block:: text

        START → analyze → select_strategy → exec_{strategy} → evaluate
                  ↑                                              │
                  └──────────── continue ────────────────────────┘
                                                                 │
                                                              done → END
    """
    graph = StateGraph(OrchestratorState)

    # Nodes
    graph.add_node("analyze", analyze_node)
    graph.add_node(
        "select_strategy",
        make_strategy_selector(spec.strategy_hints),
    )
    graph.add_node("exec_sequential", make_sequential(spec))
    graph.add_node("exec_parallel", make_parallel(spec))
    graph.add_node("exec_iterative", make_iterative(spec))
    graph.add_node("exec_planning", make_planning(spec))
    graph.add_node("evaluate", make_evaluator(spec))

    # Edges
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "select_strategy")
    graph.add_conditional_edges(
        "select_strategy",
        route_to_strategy,
        {
            "exec_sequential": "exec_sequential",
            "exec_parallel": "exec_parallel",
            "exec_iterative": "exec_iterative",
            "exec_planning": "exec_planning",
        },
    )
    for node in (
        "exec_sequential",
        "exec_parallel",
        "exec_iterative",
        "exec_planning",
    ):
        graph.add_edge(node, "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        should_continue,
        {"continue": "analyze", "done": END},
    )

    return graph.compile(name=f"orchestrated_{spec.name}")
