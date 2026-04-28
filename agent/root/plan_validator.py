"""validate_and_resolve_plans — LLM 输出的 AgentPlan 必须整批校验。"""

from __future__ import annotations

from typing import Any

from agent.skills.policy import ResolvedPolicy
from agent.sub_graphs.registry import get_default_domain, is_valid_agent_type


def _resolve_one_plan(
    raw_plan: dict[str, Any],
    policy: ResolvedPolicy,
    all_agent_ids: set[str],
) -> dict[str, Any] | None:
    agent_type = str(raw_plan.get("agent_type", ""))
    if not is_valid_agent_type(agent_type):
        return None

    skill_domain = str(raw_plan.get("skill_domain", "") or "")
    if not skill_domain:
        skill_domain = get_default_domain(agent_type) or ""

    skill_hints = list(raw_plan.get("skill_hints", []) or [])
    skill_hints = [str(h) for h in skill_hints if h]

    llm_tool_names = set(raw_plan.get("allowed_tool_names", []) or [])
    valid_tool_names = [
        s.name for s in policy.allowed_tools
        if s.name in llm_tool_names
    ]

    deps = set(raw_plan.get("depends_on", []) or [])
    if not deps.issubset(all_agent_ids):
        return None

    return {
        "agent_id": str(raw_plan.get("agent_id", "")),
        "agent_type": agent_type,
        "goal": str(raw_plan.get("goal", "")),
        "depends_on": list(deps),
        "skill_domain": skill_domain,
        "skill_hints": skill_hints,
        "allowed_tool_names": valid_tool_names,
        "max_iterations": min(
            int(raw_plan.get("max_iterations", 20)),
            policy.max_iterations,
        ),
    }


def validate_and_resolve_plans(
    raw_plans: list[dict[str, Any]],
    policy: ResolvedPolicy,
) -> list[dict[str, Any]]:
    """Validate the whole plan batch so DAG depends_on can reference peers."""
    all_agent_ids = {
        str(plan.get("agent_id", "") or "")
        for plan in raw_plans
        if str(plan.get("agent_id", "") or "")
    }
    resolved: list[dict[str, Any]] = []
    for raw_plan in raw_plans:
        item = _resolve_one_plan(raw_plan, policy, all_agent_ids)
        if item is not None:
            resolved.append(item)
    return resolved


__all__ = ["validate_and_resolve_plans"]
