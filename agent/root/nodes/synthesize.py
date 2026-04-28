"""synthesize — 汇总所有 AgentRun 结果。"""
from __future__ import annotations
from typing import Any

from agent.platform.emit import safe_emit_progress


async def synthesize(state: dict[str, Any]) -> dict[str, Any]:
    runs = dict(state.get("agent_runs", {}))
    trace_id = str(state.get("task_id", ""))

    safe_emit_progress("progress.step", {
        "content": "汇总执行结果...",
        "node": "synthesize",
        "trace_id": trace_id,
    })

    done = {k: v for k, v in runs.items() if v.get("status") == "done"}
    failed = {k: v for k, v in runs.items() if v.get("status") == "failed"}

    if not done and not failed:
        return {
            "final_result": "任务执行完成，但未产生有效结果。",
            "artifact_refs": [],
            "review": {},
            "budget": {},
            "strategy_trace": [],
        }

    if not done and failed:
        errors = "; ".join(f"{k}: {v.get('error', 'unknown')}" for k, v in failed.items())
        return {
            "final_result": f"所有 agent 执行失败: {errors}",
            "artifact_refs": [],
            "review": {"errors": errors},
            "budget": {},
            "strategy_trace": [],
        }

    parts = []
    merged_artifacts = []
    merged_review = {}
    merged_budget = {"tasks": {}}
    all_traces = []

    for agent_id, run in done.items():
        result = run.get("result") or {}
        draft = str(run.get("draft_result") or run.get("result", {}).get("final_result", ""))
        if draft:
            parts.append(f"## {run.get('goal', agent_id)[:80]}\n{draft}")
        merged_artifacts.extend(run.get("artifact_refs", []))
        if run.get("review"):
            merged_review[agent_id] = run.get("review")
        if run.get("budget"):
            merged_budget["tasks"][agent_id] = run.get("budget")
        if run.get("strategy"):
            all_traces.append(run.get("strategy"))

    final = "\n\n".join(parts) if parts else "任务已完成。"
    return {
        "final_result": final,
        "artifact_refs": merged_artifacts,
        "review": merged_review,
        "budget": merged_budget,
        "strategy_trace": list(dict.fromkeys(all_traces)),
    }
