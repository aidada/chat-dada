"""Root Graph 状态模型。

RootState: 单一编排控制面的执行账本，LangGraph checkpoint 持久化。
AgentRun: 单个 Sub Graph 执行的完整记录。
AgentPlan: Root 给 Sub Graph 的指令。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentPlan:
    agent_id: str
    agent_type: str               # → AGENT_TYPE_REGISTRY
    goal: str
    depends_on: list[str] = field(default_factory=list)
    skill_domain: str | None = None
    skill_hints: list[str] = field(default_factory=list)
    allowed_tool_names: list[str] = field(default_factory=list)
    max_iterations: int = 20


@dataclass
class AgentRun:
    agent_id: str
    agent_type: str
    status: str = "pending"       # pending | running | waiting_for_user | done | failed | cancelled
    goal: str = ""
    depends_on: list[str] = field(default_factory=list)

    latest_checkpoint_id: str | None = None
    checkpoint_ns: str = ""
    resume_metadata: dict[str, Any] | None = None
    nested_interrupt_pending: bool = False

    result: dict[str, Any] | None = None
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    error: str | None = None
    strategy: str = ""

    started_at: float | None = None
    ended_at: float | None = None
    attempt: int = 1


@dataclass
class RootState:
    task_id: str = ""
    user_id: str = ""
    original_goal: str = ""
    conversation_context: str = ""
    source_files: list[str] = field(default_factory=list)

    goal_understanding: str = ""
    execution_mode: str = "direct"    # direct | agent | dag | swarm | handoff
    scheduler_strategy: str = "single"

    agent_plans: list[dict[str, Any]] = field(default_factory=list)
    agent_runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_vars: dict[str, dict[str, Any]] = field(default_factory=dict)

    pending_question: dict[str, Any] | None = None
    interrupt_state: dict[str, Any] | None = None

    final_result: str = ""
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    strategy_trace: list[str] = field(default_factory=list)


__all__ = ["AgentPlan", "AgentRun", "RootState"]
