"""
Hybrid strategy selector: rule-based fast path + LLM fallback.

Design:
- Rules handle ~80% of cases (zero latency, zero cost)
- LLM handles ambiguous states (~20%)
- Every decision is recorded in step_history for tracing
- Follows the project's existing pattern: keyword rules + confidence scoring
  (same approach as agent/runtime/dispatcher.py and agent/platform/domain_registry.py)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel

_log = logging.getLogger("chatdada.strategy_selector")


# ── Strategy enum ────────────────────────────────────────────────────────────


class StrategyName(str, Enum):
    SEQUENTIAL = "sequential"  # 顺序
    PARALLEL = "parallel"  # 并行
    ITERATIVE = "iterative"  # 迭代
    PLANNING = "planning"  # 规划


# ── Selection result ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SelectionResult:
    strategy: StrategyName
    confidence: float  # 0.0 – 1.0
    reasoning: str
    source: str  # "rule" | "llm" | "llm_fallback"

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "source": self.source,
        }


# ── Rule-based layer ─────────────────────────────────────────────────────────
# Each rule is a pure function: (state) → SelectionResult | None
# Rules are evaluated in priority order; first match wins.
# Returning None means "I can't decide" → fall through to next rule or LLM.

_MULTI_STEP_HINTS = (
    "同时",
    "并且",
    "以及",
    "还要",
    "还需要",
    "先",
    "再",
    "分析",
    "对比",
    "综合",
    "compared",
    "analyze",
    "multiple",
)

RULE_CONFIDENCE_THRESHOLD = 0.75


def _rule_needs_refinement(state: dict[str, Any]) -> SelectionResult | None:
    """After a failed evaluation → iterative refinement."""
    evals = state.get("evaluations", [])
    if not evals:
        return None
    last = evals[-1]
    if not last.get("passed", True) and last.get("confidence", 1.0) < 0.6:
        return SelectionResult(
            strategy=StrategyName.ITERATIVE,
            confidence=0.95,
            reasoning=f"上次评审未通过 (confidence={last['confidence']:.2f})，需要迭代优化",
            source="rule",
        )
    return None


def _rule_has_pending_parallel_subtasks(state: dict[str, Any]) -> SelectionResult | None:
    """Multiple independent pending subtasks → parallel."""
    coverage = state.get("coverage", {})
    pending = [k for k, v in coverage.items() if not v]
    if len(pending) >= 2:
        return SelectionResult(
            strategy=StrategyName.PARALLEL,
            confidence=0.92,
            reasoning=f"{len(pending)} 个独立子任务待执行，适合并行",
            source="rule",
        )
    return None


def _rule_complex_goal_no_plan(state: dict[str, Any]) -> SelectionResult | None:
    """Complex goal without decomposition → dynamic planning."""
    coverage = state.get("coverage", {})
    if coverage:
        return None  # already has a plan

    goal = state.get("goal", "")
    step_history = state.get("step_history", [])

    # Don't plan twice
    if any(s.get("strategy") == "planning" for s in step_history):
        return None

    # Complexity heuristics
    complexity_signals = 0
    lowered = goal.lower()
    for hint in _MULTI_STEP_HINTS:
        if hint in lowered:
            complexity_signals += 1
    if len(goal) > 80:
        complexity_signals += 1

    if complexity_signals >= 2:
        return SelectionResult(
            strategy=StrategyName.PLANNING,
            confidence=0.88,
            reasoning=f"目标复杂 (signals={complexity_signals})，尚未分解，需要动态规划",
            source="rule",
        )
    return None


def _rule_single_pending_or_simple(state: dict[str, Any]) -> SelectionResult | None:
    """Single subtask or simple goal → sequential."""
    coverage = state.get("coverage", {})
    pending = [k for k, v in coverage.items() if not v]

    if len(pending) == 1:
        return SelectionResult(
            strategy=StrategyName.SEQUENTIAL,
            confidence=0.85,
            reasoning=f"仅剩 1 个子任务 ({pending[0]})，顺序执行",
            source="rule",
        )

    # No coverage map and simple goal
    if not coverage:
        goal = state.get("goal", "")
        if len(goal) < 60:
            return SelectionResult(
                strategy=StrategyName.SEQUENTIAL,
                confidence=0.80,
                reasoning="目标简短，顺序执行即可",
                source="rule",
            )
    return None


def _rule_domain_hints(
    state: dict[str, Any],
    strategy_hints: list[str],
) -> SelectionResult | None:
    """Domain spec provides strategy hints for first step."""
    if not strategy_hints:
        return None
    step_history = state.get("step_history", [])
    if step_history:
        return None  # hints only apply to first step

    hint = strategy_hints[0]
    try:
        strategy = StrategyName(hint)
    except ValueError:
        return None

    return SelectionResult(
        strategy=strategy,
        confidence=0.75,
        reasoning=f"领域配置建议首步使用 {hint}",
        source="rule",
    )


# Rule chain — evaluated in priority order
_RULES: list = [
    _rule_needs_refinement,  # P1: quality feedback
    _rule_has_pending_parallel_subtasks,  # P2: exploit existing plan
    _rule_complex_goal_no_plan,  # P3: create plan if needed
    _rule_single_pending_or_simple,  # P4: default sequential
    # _rule_domain_hints is handled separately (needs strategy_hints param)
]


def rule_based_select(
    state: dict[str, Any],
    strategy_hints: list[str] | None = None,
) -> SelectionResult | None:
    """Run all rules in priority order. Return first match above threshold."""
    for rule_fn in _RULES:
        result = rule_fn(state)
        if result is not None and result.confidence >= RULE_CONFIDENCE_THRESHOLD:
            _log.info(
                "Rule-based selection: %s (confidence=%.2f, rule=%s)",
                result.strategy.value,
                result.confidence,
                rule_fn.__name__,
            )
            return result

    # Try domain hints as last resort
    if strategy_hints:
        result = _rule_domain_hints(state, strategy_hints)
        if result is not None:
            return result

    _log.info("Rule-based selection: no match, falling through to LLM")
    return None


# ── LLM-based layer ──────────────────────────────────────────────────────────


class LLMStrategyDecision(BaseModel):
    """Structured output schema for LLM strategy selection."""

    strategy: str  # "sequential" | "parallel" | "iterative" | "planning"
    reasoning: str  # one-sentence justification


_STRATEGY_SELECTOR_SYSTEM = """\
你是执行策略选择器。根据当前任务状态，选择最合适的下一步执行策略。

可选策略：
- sequential: 顺序执行，适合线性任务或单一子任务
- parallel: 并行执行，适合多个独立子任务同时进行
- iterative: 迭代优化，适合上次输出质量不足需要改进
- planning: 动态规划，适合复杂任务需要先分解再执行

选择原则：
1. 评审未通过 → iterative（根据反馈改进）
2. 多个独立子任务 → parallel（提高效率）
3. 复杂目标未分解 → planning（先规划再执行）
4. 简单或单一任务 → sequential（直接执行）

输出 JSON：{"strategy": "...", "reasoning": "..."}"""


async def llm_select(state: dict[str, Any]) -> SelectionResult:
    """LLM fallback for ambiguous states."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from core.models import get_llm

    evals = state.get("evaluations", [])
    last_eval_str = "无"
    if evals:
        last = evals[-1]
        last_eval_str = (
            f"passed={last.get('passed')}, "
            f"confidence={last.get('confidence', 0):.2f}, "
            f"issues={len(last.get('issues', []))}"
        )

    coverage = state.get("coverage", {})
    pending = [k for k, v in coverage.items() if not v]
    completed = [k for k, v in coverage.items() if v]

    prompt = (
        f"当前状态：\n"
        f"- 目标：{state.get('goal', '')[:200]}\n"
        f"- 进度：{state.get('progress', 0):.0%}\n"
        f"- 信心度：{state.get('confidence', 0):.0%}\n"
        f"- 已完成子任务：{completed or '无'}\n"
        f"- 待完成子任务：{pending or '无'}\n"
        f"- 累计成本：${state.get('cost', 0):.3f}\n"
        f"- 已执行步骤：{len(state.get('step_history', []))}\n"
        f"- 上次评审：{last_eval_str}\n"
        f"\n请选择下一步执行策略。"
    )

    try:
        llm = get_llm("orchestrator")
        structured_llm = llm.with_structured_output(LLMStrategyDecision)
        decision: LLMStrategyDecision = await structured_llm.ainvoke(
            [
                SystemMessage(content=_STRATEGY_SELECTOR_SYSTEM),
                HumanMessage(content=prompt),
            ]
        )
        strategy = StrategyName(decision.strategy)
        return SelectionResult(
            strategy=strategy,
            confidence=0.70,
            reasoning=f"LLM: {decision.reasoning}",
            source="llm",
        )
    except Exception as exc:
        _log.warning("LLM strategy selection failed: %s, defaulting to sequential", exc)
        return SelectionResult(
            strategy=StrategyName.SEQUENTIAL,
            confidence=0.50,
            reasoning=f"LLM 选择失败 ({exc})，降级为顺序执行",
            source="llm_fallback",
        )


# ── Hybrid entry point (LangGraph node factory) ─────────────────────────────


def make_strategy_selector(strategy_hints: list[str] | None = None):
    """Factory: returns a LangGraph node function for strategy selection.

    The node reads ``OrchestratorState`` and writes ``selected_strategy``
    plus an entry in ``step_history`` for tracing.
    """

    async def select_strategy_node(state: dict[str, Any]) -> dict[str, Any]:
        # Layer 1: Rules
        result = rule_based_select(state, strategy_hints)

        # Layer 2: LLM (only if rules can't decide)
        if result is None:
            result = await llm_select(state)

        _log.info(
            "Strategy selected: %s (confidence=%.2f, source=%s) — %s",
            result.strategy.value,
            result.confidence,
            result.source,
            result.reasoning,
        )

        try:
            from langgraph.config import get_stream_writer

            get_stream_writer()(
                {
                    "event_type": "strategy",
                    "strategy": result.strategy.value,
                    "confidence": result.confidence,
                    "reasoning": result.reasoning,
                    "source": result.source,
                    "content": f"Strategy selected: {result.strategy.value}",
                }
            )
        except Exception:
            pass

        return {
            "selected_strategy": result.strategy.value,
            "step_history": [result.to_dict()],
        }

    return select_strategy_node
