"""understand_goal — LLM 理解意图 → 生成 AgentPlan 列表。"""
from __future__ import annotations
import json
import logging
from typing import Any

from agent.platform.emit import safe_emit_progress
from agent.root.scheduler import SCHEDULER_STRATEGIES
from core.models import get_llm, response_text

_log = logging.getLogger("chatdada.root.understand_goal")

_EXECUTION_MODES_DESC = """
可用编排模式:
- direct: 直接回答，无需创建 agent
- agent: 创建单个 agent 执行
- dag: 多个 agent，有依赖关系
- swarm: 多个 agent 并行执行，无依赖
- handoff: 多个 agent 按顺序传递
"""


async def understand_goal(state: dict[str, Any]) -> dict[str, Any]:
    goal = str(state.get("original_goal", "") or state.get("task_text", ""))
    trace_id = str(state.get("task_id", ""))

    safe_emit_progress("progress.step", {
        "content": "理解目标...",
        "node": "understand_goal",
        "trace_id": trace_id,
    })

    llm = get_llm("orchestrator")
    from langchain_core.messages import HumanMessage, SystemMessage

    prompt = (
        f"你是一个任务编排器。根据用户目标，判断执行模式并生成 AgentPlan。\n\n"
        f"{_EXECUTION_MODES_DESC}\n\n"
        f"可用 agent_type: research, patent, office, writer, analyst\n\n"
        f"输出 JSON：\n"
        f'{{"execution_mode": "...", "goal_understanding": "...", '
        f'"scheduler_strategy": "...", '
        f'"agent_plans": [{{"agent_id": "...", "agent_type": "...", '
        f'"goal": "...", "depends_on": [], '
        f'"skill_domain": "...", "skill_hints": [], '
        f'"allowed_tool_names": ["..."]}}]}}'
    )

    try:
        response = await llm.ainvoke([
            SystemMessage(content=prompt),
            HumanMessage(content=f"目标: {goal}"),
        ])
        text = response_text(response).strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3].strip()
        parsed = json.loads(text)

        return {
            "goal_understanding": str(parsed.get("goal_understanding", goal)),
            "execution_mode": str(parsed.get("execution_mode", "direct")),
            "scheduler_strategy": str(parsed.get("scheduler_strategy", "single")),
            "agent_plans": parsed.get("agent_plans", []),
        }
    except Exception as exc:
        _log.exception("understand_goal failed, fallback to direct: %s", exc)
        return {
            "goal_understanding": goal,
            "execution_mode": "direct",
            "scheduler_strategy": "single",
            "agent_plans": [],
        }
