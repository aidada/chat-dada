"""Sub Graph 状态模型。

AgentState: Sub Graph 内部的 ReAct 循环状态，LangGraph checkpoint 持久化。
SkillContext: Root → Sub Graph 传入的标准 envelope。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillContext:
    """Root 传给 Sub Graph 的标准上下文 envelope。

    只包含可序列化的 id/scope/摘要——不包含 SkillLoader/ToolGateway
    等运行时对象。运行时对象通过 LangGraph configurable 传入。
    """

    agent_id: str
    root_task_id: str
    root_user_id: str
    checkpoint_ns: str
    trace_id: str
    skill_domain: str | None = None
    skill_hints: list[str] = field(default_factory=list)
    allowed_tool_names: list[str] = field(default_factory=list)
    latest_checkpoint_id: str | None = None
    resume_metadata: dict[str, Any] = field(default_factory=dict)
    upstream: dict[str, Any] | None = None


@dataclass
class AgentState:
    """Sub Graph ReAct 循环的持久化状态。"""

    agent_id: str = ""
    goal: str = ""
    max_iterations: int = 20

    messages: list[dict[str, Any]] = field(default_factory=list)
    decision_summary: str = ""
    selected_skill: str | None = None
    action: str | None = None
    observation_summary: str = ""
    active_tool_calls: list[dict[str, Any]] = field(default_factory=list)

    draft_result: str = ""
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    strategy_trace: list[str] = field(default_factory=list)

    iteration: int = 0
    status: str = "running"
    error: str | None = None


__all__ = ["AgentState", "SkillContext"]
