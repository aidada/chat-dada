"""build_agents — 校验 AgentPlan + 构造 SkillContext + 初始化 AgentRun。"""
from __future__ import annotations
import logging
from typing import Any

from langgraph.config import get_config

from agent.root.plan_validator import validate_and_resolve_plans
from agent.root.scheduler import get_strategy
from agent.sub_graphs.state import SkillContext

_log = logging.getLogger("chatdada.root.build_agents")


async def build_agents(state: dict[str, Any]) -> dict[str, Any]:
    raw_plans = state.get("agent_plans", [])
    if not raw_plans:
        return {"agent_plans": [], "agent_runs": {}, "task_vars": {}}

    cfg = get_config()
    configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
    policy = configurable.get("resolved_policy")
    validated_plans = validate_and_resolve_plans(raw_plans, policy) if policy else list(raw_plans)
    agent_runs = dict(state.get("agent_runs", {}))

    for validated in validated_plans:
        agent_id = validated["agent_id"]
        plan = validated

        agent_runs[agent_id] = {
            "agent_id": agent_id,
            "agent_type": plan["agent_type"],
            "status": "pending",
            "goal": plan["goal"],
            "depends_on": plan.get("depends_on", []),
            "latest_checkpoint_id": None,
            "checkpoint_ns": agent_id,
            "resume_metadata": None,
            "nested_interrupt_pending": False,
            "result": None,
            "artifact_refs": [],
            "review": None,
            "budget": None,
            "error": None,
            "strategy": "",
            "started_at": None,
            "ended_at": None,
            "attempt": 1,
        }

    return {
        "agent_plans": validated_plans,
        "agent_runs": agent_runs,
        "task_vars": dict(state.get("task_vars", {})),
    }
