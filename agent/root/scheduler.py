"""Scheduler strategies — 多模式编排的 ready_check 逻辑。

新增模式：在 SCHEDULER_STRATEGIES 加配置 + 实现 ready_check 函数。
"""

from __future__ import annotations

from typing import Any


def _is_dep_satisfied(agent_id: str, plan: dict, runs: dict[str, dict]) -> bool:
    for dep_id in plan.get("depends_on", []):
        dep_run = runs.get(dep_id, {})
        if dep_run.get("status") != "done":
            return False
    return True


def ready_all_pending(
    plans: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> list[str]:
    """Swarm: 所有 pending agent 同时就绪。"""
    return [p["agent_id"] for p in plans
            if runs.get(p["agent_id"], {}).get("status") == "pending"]


def ready_dependencies_satisfied(
    plans: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> list[str]:
    """DAG: 仅依赖已满足的 agent 就绪。"""
    ready_ids = []
    for p in plans:
        rid = p["agent_id"]
        run = runs.get(rid, {})
        if run.get("status") != "pending":
            continue
        if _is_dep_satisfied(rid, p, runs):
            ready_ids.append(rid)
    return ready_ids


def ready_next_in_chain(
    plans: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> list[str]:
    """Handoff: 按线性顺序，取第一个 pending。"""
    for p in plans:
        rid = p["agent_id"]
        run = runs.get(rid, {})
        if run.get("status") != "pending":
            continue
        if _is_dep_satisfied(rid, p, runs):
            return [rid]
    return []


def ready_first_pending(
    plans: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> list[str]:
    """Single: 取第一个 pending agent。"""
    for p in plans:
        if runs.get(p["agent_id"], {}).get("status") == "pending":
            return [p["agent_id"]]
    return []


SCHEDULER_STRATEGIES: dict[str, dict[str, Any]] = {
    "dependency_graph": {
        "ready_check": ready_dependencies_satisfied,
        "parallel": True,
        "loop": True,
    },
    "parallel": {
        "ready_check": ready_all_pending,
        "parallel": True,
        "loop": False,
    },
    "sequential": {
        "ready_check": ready_next_in_chain,
        "parallel": False,
        "loop": True,
    },
    "single": {
        "ready_check": ready_first_pending,
        "parallel": False,
        "loop": False,
    },
}


def get_strategy(name: str) -> dict[str, Any]:
    return SCHEDULER_STRATEGIES.get(name, SCHEDULER_STRATEGIES["single"])


def get_ready_agents(
    plans: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    strategy_name: str,
) -> list[str]:
    strategy = get_strategy(strategy_name)
    return strategy["ready_check"](plans, runs)


__all__ = [
    "SCHEDULER_STRATEGIES",
    "get_ready_agents",
    "get_strategy",
    "ready_all_pending",
    "ready_dependencies_satisfied",
    "ready_first_pending",
    "ready_next_in_chain",
]
