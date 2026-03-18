"""
Hierarchical research planner — decomposes a research query into subtasks.

Generates a structured plan with 2-5 subtasks, each with search angles,
dependencies, priority, and completion criteria.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from models import get_llm, response_text

log = logging.getLogger("chatdada.research_planner")

PLAN_VERSION = 1
SUBTASK_VERSION = 1

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ResearchSubtask:
    id: str = ""
    topic: str = ""
    search_angles: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    priority: int = 2  # 1=high, 2=medium, 3=low
    max_rounds: int = 3
    status: str = "pending"  # pending / in_progress / completed / skipped
    completion_criteria: str = ""
    findings_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "_version": SUBTASK_VERSION,
            "id": self.id,
            "topic": self.topic,
            "search_angles": list(self.search_angles),
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "max_rounds": self.max_rounds,
            "status": self.status,
            "completion_criteria": self.completion_criteria,
            "findings_summary": self.findings_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResearchSubtask:
        version = data.get("_version", 0)
        if version != SUBTASK_VERSION:
            log.warning("ResearchSubtask version mismatch: expected %d, got %d", SUBTASK_VERSION, version)
        return cls(
            id=data.get("id", ""),
            topic=data.get("topic", ""),
            search_angles=list(data.get("search_angles", [])),
            depends_on=list(data.get("depends_on", [])),
            priority=data.get("priority", 2),
            max_rounds=data.get("max_rounds", 3),
            status=data.get("status", "pending"),
            completion_criteria=data.get("completion_criteria", ""),
            findings_summary=data.get("findings_summary", ""),
        )


@dataclass
class ResearchPlan:
    original_query: str = ""
    clarified_goal: str = ""
    subtasks: list[ResearchSubtask] = field(default_factory=list)
    global_constraints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "_version": PLAN_VERSION,
            "original_query": self.original_query,
            "clarified_goal": self.clarified_goal,
            "subtasks": [st.to_dict() for st in self.subtasks],
            "global_constraints": list(self.global_constraints),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResearchPlan:
        version = data.get("_version", 0)
        if version != PLAN_VERSION:
            log.warning("ResearchPlan version mismatch: expected %d, got %d", PLAN_VERSION, version)
        return cls(
            original_query=data.get("original_query", ""),
            clarified_goal=data.get("clarified_goal", ""),
            subtasks=[ResearchSubtask.from_dict(s) for s in data.get("subtasks", [])],
            global_constraints=list(data.get("global_constraints", [])),
        )


# ---------------------------------------------------------------------------
# JSON parsing helper
# ---------------------------------------------------------------------------


def _parse_plan_json(text: str) -> dict:
    """Extract JSON from LLM response, handling markdown code blocks."""
    # Try to extract from ```json ... ``` block
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    # Try direct JSON parse
    return json.loads(text)


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------

PLAN_SYSTEM_PROMPT = """你是一个研究规划器。根据用户的研究问题，生成一个结构化的研究计划。

要求：
1. 拆分为 2-5 个子任务
2. 每个子任务有明确的主题、搜索角度、依赖关系和完成标准
3. 优先级 1=高 2=中 3=低
4. 子任务之间的依赖关系要合理

请以 JSON 格式返回：
```json
{
  "clarified_goal": "明确的研究目标",
  "subtasks": [
    {
      "id": "sub_1",
      "topic": "子任务主题",
      "search_angles": ["搜索角度1", "搜索角度2"],
      "depends_on": [],
      "priority": 1,
      "max_rounds": 3,
      "completion_criteria": "何时认为该子任务完成"
    }
  ],
  "global_constraints": ["全局约束1"]
}
```"""


async def generate_research_plan(
    query: str,
    memory_context: str = "",
    report_profile: str = "default",
) -> ResearchPlan:
    """Generate a research plan with 2-5 subtasks."""
    if not query or not query.strip():
        raise ValueError("Research query cannot be empty")
    query = query.strip()
    llm = get_llm("orchestrator")

    human_prompt = f"研究问题：{query}"
    if memory_context:
        human_prompt += f"\n\n已有背景信息：{memory_context}"
    if report_profile != "default":
        human_prompt += f"\n\n输出模板：{report_profile}"

    resp = await llm.ainvoke([
        SystemMessage(content=PLAN_SYSTEM_PROMPT),
        HumanMessage(content=human_prompt),
    ])

    text = response_text(resp)
    try:
        plan_data = _parse_plan_json(text)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to parse research plan JSON: %s", e)
        # Fallback: create a single-subtask plan
        return ResearchPlan(
            original_query=query,
            clarified_goal=query,
            subtasks=[ResearchSubtask(
                id="sub_1",
                topic=query,
                search_angles=[query],
                priority=1,
                max_rounds=5,
                completion_criteria="信息足够回答原始问题",
            )],
        )

    plan = ResearchPlan(
        original_query=query,
        clarified_goal=plan_data.get("clarified_goal", query),
        subtasks=[ResearchSubtask.from_dict(s) for s in plan_data.get("subtasks", [])],
        global_constraints=list(plan_data.get("global_constraints", [])),
    )

    # Ensure at least one subtask
    if not plan.subtasks:
        plan.subtasks.append(ResearchSubtask(
            id="sub_1",
            topic=query,
            search_angles=[query],
            priority=1,
            completion_criteria="信息足够回答原始问题",
        ))

    return plan


# ---------------------------------------------------------------------------
# Plan navigation
# ---------------------------------------------------------------------------


def get_next_subtask(plan: ResearchPlan) -> ResearchSubtask | None:
    """Return the next pending subtask whose dependencies are satisfied.

    Picks highest priority (lowest number) among eligible subtasks.
    """
    completed_ids = {st.id for st in plan.subtasks if st.status in ("completed", "skipped")}

    eligible: list[ResearchSubtask] = []
    for st in plan.subtasks:
        if st.status != "pending":
            continue
        # Check all dependencies are satisfied
        if all(dep in completed_ids for dep in st.depends_on):
            eligible.append(st)

    if not eligible:
        return None

    # Sort by priority (1=high first)
    eligible.sort(key=lambda s: s.priority)
    return eligible[0]


def is_plan_complete(plan: ResearchPlan) -> bool:
    """Check if all subtasks are completed or skipped."""
    return all(st.status in ("completed", "skipped") for st in plan.subtasks)
