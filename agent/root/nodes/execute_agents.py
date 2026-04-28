"""execute_agents — 执行就绪的 Sub Graphs。"""
from __future__ import annotations
import asyncio
import logging
import uuid
from typing import Any

from agent.platform.streaming import stream_nested_graph
from agent.platform.emit import safe_emit_progress
from agent.root.scheduler import get_strategy, get_ready_agents
from agent.sub_graphs.registry import AGENT_TYPE_REGISTRY
from agent.sub_graphs.state import SkillContext

_log = logging.getLogger("chatdada.root.execute_agents")


async def execute_agents(state: dict[str, Any]) -> dict[str, Any]:
    from langgraph.config import get_config

    cfg = get_config()
    configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
    plans = state.get("agent_plans", [])
    runs = dict(state.get("agent_runs", {}))
    strategy_name = str(state.get("scheduler_strategy", "single"))
    task_id = str(state.get("task_id", ""))
    user_id = str(state.get("user_id", ""))
    trace_id = str(state.get("task_id", ""))

    ready_ids = get_ready_agents(plans, runs, strategy_name)
    if not ready_ids:
        return {"agent_runs": runs}

    strategy = get_strategy(strategy_name)
    parallel = strategy.get("parallel", False)

    async def run_one(plan: dict[str, Any]) -> dict[str, Any]:
        agent_id = plan["agent_id"]
        agent_type = plan["agent_type"]

        factory = AGENT_TYPE_REGISTRY.get(agent_type)
        if factory is None:
            return {
                "agent_id": agent_id,
                "status": "failed",
                "error": f"Unknown agent_type: {agent_type}",
            }

        graph = factory()
        skill_context = SkillContext(
            agent_id=agent_id,
            root_task_id=task_id,
            root_user_id=user_id,
            checkpoint_ns=agent_id,
            trace_id=f"{trace_id}:{agent_id}",
            skill_domain=plan.get("skill_domain"),
            skill_hints=plan.get("skill_hints", []),
            allowed_tool_names=plan.get("allowed_tool_names", []),
        )

        runs[agent_id]["status"] = "running"
        runs[agent_id]["started_at"] = __import__("time").monotonic()

        safe_emit_progress("progress.step", {
            "content": f"开始执行: {plan.get('goal', agent_id)[:80]}",
            "agent_id": agent_id,
            "agent_type": agent_type,
            "trace_id": trace_id,
        })

        try:
            result = await stream_nested_graph(
                graph,
                {"goal": plan["goal"], "agent_id": agent_id, "max_iterations": plan.get("max_iterations", 20)},
                config={"configurable": {
                    "thread_id": task_id,
                    "checkpoint_ns": agent_id,
                    "skill_context": skill_context,
                    "skill_loader": configurable.get("skill_loader"),
                    "tool_gateway": configurable.get("tool_gateway"),
                    "resolved_policy": configurable.get("resolved_policy"),
                }},
                extra_payload={"nested_graph": agent_id, "agent_id": agent_id},
            )
        except Exception as exc:
            exc_name = type(exc).__name__
            if "GraphInterrupt" in exc_name or "Interrupt" in exc_name:
                runs[agent_id]["status"] = "waiting_for_user"
                return {"agent_id": agent_id, "status": "waiting_for_user"}
            runs[agent_id]["status"] = "failed"
            runs[agent_id]["error"] = str(exc)
            runs[agent_id]["ended_at"] = __import__("time").monotonic()
            return {"agent_id": agent_id, "status": "failed", "error": str(exc)}

        runs[agent_id]["status"] = "done"
        runs[agent_id]["ended_at"] = __import__("time").monotonic()

        if isinstance(result, dict):
            if result.get("status") == "waiting_for_user" or result.get("resume_metadata"):
                runs[agent_id]["status"] = "waiting_for_user"
                runs[agent_id]["resume_metadata"] = dict(result.get("resume_metadata") or {})
                runs[agent_id]["latest_checkpoint_id"] = result.get("latest_checkpoint_id")
                return {"agent_id": agent_id, "status": "waiting_for_user"}
            runs[agent_id]["result"] = result
            runs[agent_id]["draft_result"] = result.get("final_result") or result.get("draft_result", "")
            runs[agent_id]["artifact_refs"] = result.get("artifact_refs", [])
            runs[agent_id]["review"] = result.get("review")
            runs[agent_id]["budget"] = result.get("budget")
            runs[agent_id]["strategy"] = result.get("strategy", "")

        return {"agent_id": agent_id, "status": "done"}

    ready_plans = [p for p in plans if p["agent_id"] in ready_ids]
    if parallel:
        results = await asyncio.gather(*[run_one(p) for p in ready_plans])
    else:
        results = []
        for p in ready_plans:
            r = await run_one(p)
            results.append(r)

    task_vars = dict(state.get("task_vars", {}))
    for plan in ready_plans:
        agent_id = plan["agent_id"]
        run = runs.get(agent_id, {})
        task_vars[agent_id] = {
            "title": str(plan.get("goal", ""))[:80],
            "summary": str(run.get("result", {}).get("final_result", ""))[:500],
            "upstream_artifacts": run.get("artifact_refs", []),
        }

    return {"agent_runs": runs, "task_vars": task_vars}
