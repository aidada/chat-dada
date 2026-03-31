"""
DomainSpec — the single interface between domain agents and the orchestrator.

A domain agent declares WHAT it knows (prompts, tools, subagents, evaluator).
The orchestrator decides HOW to execute (strategy selection, state management).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.capabilities.review_gates import ReviewGate


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


@dataclass
class DomainSpec:
    """Everything a domain agent needs to declare to opt into the orchestrator.

    The orchestrator uses this to build a LangGraph graph that dynamically
    selects and composes execution strategies (sequential, parallel, iterative,
    planning) using ``deepagents.create_deep_agent()`` as the agent harness.
    """

    name: str                                        # "research", "patent", etc.
    model_role: str                                  # key in core.models.MODEL_CONFIGS
    system_prompt: str                               # main agent system prompt
    tools: list[Any] = field(default_factory=list)
    subagents: list[SubagentConfig] = field(default_factory=list)
    evaluator: ReviewGate = field(default_factory=ReviewGate)
    report_profile: str = ""
    strategy_hints: list[str] = field(default_factory=list)  # first-step preference
    max_steps: int = 10
    max_cost: float = 5.0                            # USD budget cap
