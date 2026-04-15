# tests/test_coordinator_phase2_events.py
"""Phase 2 event sequence compatibility tests."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from agent.coordinator.state import CoordinatorState


def _mock_stream_writer():
    """Returns (captured_events list, writer function)."""
    captured: list[dict[str, Any]] = []

    def writer(event: dict[str, Any]) -> None:
        captured.append(event)

    return captured, writer


@pytest.mark.asyncio
async def test_coordinator_emits_task_dag_event_in_dag_mode():
    """DAG mode must emit 'task_dag' event with tasks list."""
    captured, writer = _mock_stream_writer()

    # skill_registry and _call_llm_json are imported inside the function body,
    # so we patch them at their source module (agent.coordinator.skills / executor).
    with patch("agent.coordinator.executor.get_stream_writer", return_value=writer), \
         patch("agent.coordinator.executor._call_llm_json", new=AsyncMock(return_value={
             "tasks": [
                 {"id": "t1", "title": "研究", "description": "调研", "depends_on": [],
                  "assigned_skill": "do_research", "input_data": {"query": "test"}},
             ]
         })):
        from agent.coordinator.executor import decompose_tasks_node
        state: CoordinatorState = {
            "original_goal": "研究量子计算",
            "trace_id": "trace-dag-001",
            "clarification_history": [],
        }
        with patch("agent.coordinator.skills.skill_registry") as mock_reg:
            mock_reg.skill_summary_for_llm.return_value = "技能列表"
            await decompose_tasks_node(state)

    dag_events = [e for e in captured if e.get("event_type") == "task_dag"]
    assert len(dag_events) == 1
    assert dag_events[0]["status"] == "generated"
    assert len(dag_events[0]["tasks"]) == 1
    assert dag_events[0]["tasks"][0]["id"] == "t1"


@pytest.mark.asyncio
async def test_coordinator_emits_task_start_and_complete_events():
    """execute_tasks_node must emit task_start and task_complete for each task."""
    captured, writer = _mock_stream_writer()

    from agent.coordinator.state import CoordinatorConfig, Task, SkillResult

    task = Task(
        id="t1", title="研究", assigned_skill="do_research",
        input_data={"query": "test"}, depends_on=[]
    )

    mock_skill_result = SkillResult(
        status="ok",
        result="done",
        artifact_refs=[],
        review={},
        budget={},
    )

    state: CoordinatorState = {
        "task_dag": [task],
        "pending_tasks": ["t1"],
        "completed_tasks": {},
        "failed_tasks": {},
        "running_tasks": {},
        "task_vars": {},
        "skill_runs": {},
        "trace_id": "trace-exec-001",
        "config": CoordinatorConfig(),
        "clarification_history": [],
    }

    with patch("agent.coordinator.executor.get_stream_writer", return_value=writer), \
         patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter", new=AsyncMock(return_value=mock_skill_result)):
        mock_reg.get_runner.return_value = MagicMock()
        from agent.coordinator.executor import execute_tasks_node
        await execute_tasks_node(state)

    event_types = [e.get("event_type") for e in captured]
    assert "task_start" in event_types, f"Missing task_start. Got: {event_types}"
    assert "task_complete" in event_types, f"Missing task_complete. Got: {event_types}"

    start_event = next(e for e in captured if e.get("event_type") == "task_start")
    assert start_event["task_id"] == "t1"
    assert start_event["skill"] == "do_research"

    complete_event = next(e for e in captured if e.get("event_type") == "task_complete")
    assert complete_event["task_id"] == "t1"
    assert complete_event["status"] == "ok"


@pytest.mark.asyncio
async def test_coordinator_additive_events_do_not_replace_standard_events():
    """Coordinator events (task_dag, task_start, task_complete) are additive only.
    Standard domain events (step, checkpoint, etc.) from nested skill are preserved."""
    captured, writer = _mock_stream_writer()

    from agent.coordinator.state import CoordinatorConfig, Task, SkillResult

    async def domain_runner_with_events_adapter(runner, input_data, context):
        # Simulate a domain runner that emits its own events via the shared writer
        writer({"event_type": "step", "content": "领域技能步骤", "source": "research_workflow"})
        writer({"event_type": "checkpoint", "checkpoint_id": "cp_001", "source": "research_workflow"})
        return SkillResult(status="ok", result="研究完成", artifact_refs=[], review={}, budget={})

    task = Task(id="t1", title="T", assigned_skill="do_research",
                input_data={"query": "test"}, depends_on=[])
    state: CoordinatorState = {
        "task_dag": [task], "pending_tasks": ["t1"], "completed_tasks": {},
        "failed_tasks": {}, "running_tasks": {}, "task_vars": {}, "skill_runs": {},
        "trace_id": "trace-additive-001", "config": CoordinatorConfig(), "clarification_history": [],
    }

    with patch("agent.coordinator.executor.get_stream_writer", return_value=writer), \
         patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter", new=domain_runner_with_events_adapter):
        mock_reg.get_runner.return_value = MagicMock()
        from agent.coordinator.executor import execute_tasks_node
        await execute_tasks_node(state)

    event_types = [e.get("event_type") for e in captured]
    # Coordinator additive events
    assert "task_start" in event_types
    assert "task_complete" in event_types
    # Domain standard events preserved
    assert "step" in event_types, "Domain step events must not be suppressed"
    assert "checkpoint" in event_types, "Domain checkpoint events must not be suppressed"


@pytest.mark.asyncio
async def test_skill_runs_tracking_after_execution():
    """skill_runs dict must be populated with status=done after successful task."""
    from agent.coordinator.state import CoordinatorConfig, Task, SkillResult

    task = Task(id="t1", title="研究", assigned_skill="do_research",
                input_data={"query": "test"}, depends_on=[])

    mock_skill_result = SkillResult(
        status="ok",
        result="完成",
        artifact_refs=[],
        review={},
        budget={},
    )

    state: CoordinatorState = {
        "task_dag": [task], "pending_tasks": ["t1"], "completed_tasks": {},
        "failed_tasks": {}, "running_tasks": {}, "task_vars": {}, "skill_runs": {},
        "trace_id": "trace-runs-001", "config": CoordinatorConfig(), "clarification_history": [],
    }

    with patch("agent.coordinator.executor.get_stream_writer", return_value=lambda _: None), \
         patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter", new=AsyncMock(return_value=mock_skill_result)):
        mock_reg.get_runner.return_value = MagicMock()
        from agent.coordinator.executor import execute_tasks_node
        result = await execute_tasks_node(state)

    skill_runs = result.get("skill_runs", {})
    assert "t1" in skill_runs
    assert skill_runs["t1"]["status"] == "done"
