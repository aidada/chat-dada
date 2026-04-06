"""Phase 2 checkpoint coexistence tests — skill_runs tracking and upstream context injection."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from agent.coordinator.state import CoordinatorConfig, CoordinatorState, SkillResult, Task


# ── skill_runs DAG-level checkpoint tracking ─────────────────────────────────

@pytest.mark.asyncio
async def test_skill_runs_stores_done_status_after_successful_task():
    task = Task(id="t1", title="研究", assigned_skill="do_research",
                input_data={"query": "test"}, depends_on=[])

    async def runner(input_data: dict) -> dict:
        return {"result": "研究结果", "artifact_refs": [], "review": {}, "budget": {}}

    state: CoordinatorState = {
        "task_dag": [task], "pending_tasks": ["t1"], "completed_tasks": {},
        "failed_tasks": {}, "running_tasks": {}, "task_vars": {}, "skill_runs": {},
        "trace_id": "trace-ckpt-001", "config": CoordinatorConfig(), "clarification_history": [],
    }

    ok_result = SkillResult(status="ok", result="研究结果", artifact_refs=[], review={}, budget={})

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter", return_value=ok_result), \
         patch("agent.coordinator.executor.get_stream_writer", return_value=lambda _: None):
        mock_reg.get_runner.return_value = runner
        from agent.coordinator.executor import execute_tasks_node
        result = await execute_tasks_node(state)

    skill_runs = result.get("skill_runs", {})
    assert "t1" in skill_runs
    assert skill_runs["t1"]["status"] == "done"


@pytest.mark.asyncio
async def test_skill_runs_stores_checkpoint_fields_after_successful_task():
    """skill_runs entry must include latest_checkpoint_id and resume_metadata fields."""
    task = Task(id="t1", title="研究", assigned_skill="do_research",
                input_data={"query": "test"}, depends_on=[])

    async def runner(input_data: dict) -> dict:
        return {"result": "研究结果", "artifact_refs": [], "review": {}, "budget": {}}

    state: CoordinatorState = {
        "task_dag": [task], "pending_tasks": ["t1"], "completed_tasks": {},
        "failed_tasks": {}, "running_tasks": {}, "task_vars": {}, "skill_runs": {},
        "trace_id": "trace-ckpt-002", "config": CoordinatorConfig(), "clarification_history": [],
    }

    ok_result = SkillResult(
        status="ok", result="研究结果", artifact_refs=[], review={}, budget={},
        latest_checkpoint_id=None, resume_metadata={},
    )

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter", return_value=ok_result), \
         patch("agent.coordinator.executor.get_stream_writer", return_value=lambda _: None):
        mock_reg.get_runner.return_value = runner
        from agent.coordinator.executor import execute_tasks_node
        result = await execute_tasks_node(state)

    skill_runs = result.get("skill_runs", {})
    assert "t1" in skill_runs
    # Fields must exist (values may be None if domain doesn't provide them)
    assert "latest_checkpoint_id" in skill_runs["t1"]
    assert "resume_metadata" in skill_runs["t1"]


@pytest.mark.asyncio
async def test_skill_runs_stores_error_on_failure():
    task = Task(id="t1", title="研究失败", assigned_skill="do_research",
                input_data={"query": "test"}, depends_on=[])

    async def failing_runner(input_data: dict) -> dict:
        raise RuntimeError("LLM quota exceeded")

    state: CoordinatorState = {
        "task_dag": [task], "pending_tasks": ["t1"], "completed_tasks": {},
        "failed_tasks": {}, "running_tasks": {}, "task_vars": {}, "skill_runs": {},
        "trace_id": "trace-fail-001", "config": CoordinatorConfig(), "clarification_history": [],
    }

    error_result = SkillResult(status="error", error="LLM quota exceeded")

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter", return_value=error_result), \
         patch("agent.coordinator.executor.get_stream_writer", return_value=lambda _: None):
        mock_reg.get_runner.return_value = failing_runner
        from agent.coordinator.executor import execute_tasks_node
        result = await execute_tasks_node(state)

    skill_runs = result.get("skill_runs", {})
    assert "t1" in skill_runs
    assert skill_runs["t1"]["status"] in ("failed", "error")
    assert skill_runs["t1"].get("error") is not None


# ── build_task_vars_entry and inject_upstream_context ─────────────────────────

def test_build_task_vars_entry_from_skill_result():
    """build_task_vars_entry produces correct TaskVarEntry structure."""
    from agent.coordinator.executor import build_task_vars_entry

    task = Task(id="t1", title="研究", assigned_skill="do_research",
                input_data={"query": "test"}, depends_on=[])
    result = SkillResult(
        status="ok",
        result="研究结论：量子计算在工程领域有重大进展。",
        artifact_refs=[{"name": "report.md"}],
        review={"passed": True},
        budget={"cost": 1.5},
        strategy="parallel",
    )

    entry = build_task_vars_entry(task, result)
    assert entry.summary == "研究结论：量子计算在工程领域有重大进展。"
    assert entry.artifact_refs == [{"name": "report.md"}]
    assert entry.source_task_id == "t1"
    assert entry.source_skill == "do_research"


def test_build_task_vars_entry_truncates_long_summary():
    from agent.coordinator.executor import build_task_vars_entry

    task = Task(id="t1", title="T", assigned_skill="do_research",
                input_data={}, depends_on=[])
    long_result = "X" * 3000
    result = SkillResult(status="ok", result=long_result, artifact_refs=[], review={}, budget={})

    entry = build_task_vars_entry(task, result)
    assert len(entry.summary) <= 2000
    assert entry.summary.endswith("...")


def test_inject_upstream_context_contains_summary():
    """inject_upstream_context includes upstream summaries in returned dict."""
    from agent.coordinator.state import TaskVarEntry
    from agent.coordinator.executor import inject_upstream_context

    task = Task(id="t2", title="专利", assigned_skill="do_patent",
                input_data={"query": "专利"}, depends_on=["t1"])
    task_vars = {
        "t1": TaskVarEntry(
            summary="研究结论：量子计算",
            artifact_refs=[{"name": "research.md"}],
            source_task_id="t1",
            source_skill="do_research",
        )
    }

    injected = inject_upstream_context(task, task_vars)
    assert "upstream_context" in injected
    assert "量子计算" in injected["upstream_context"]
    assert injected["upstream_artifacts"] == [{"name": "research.md"}]


def test_inject_upstream_context_empty_when_no_deps():
    from agent.coordinator.executor import inject_upstream_context

    task = Task(id="t1", title="T", assigned_skill="do_research",
                input_data={}, depends_on=[])
    injected = inject_upstream_context(task, {})
    assert injected["upstream_context"] == ""
    assert injected["upstream_artifacts"] == []
