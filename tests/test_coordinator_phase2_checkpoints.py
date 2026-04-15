"""Phase 2 checkpoint coexistence tests — skill_runs tracking and upstream context injection."""
from __future__ import annotations

import json
import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.mark.asyncio
async def test_coordinator_graph_checkpoint_state_contains_recovery_fields():
    """Checkpoint snapshot should retain DAG recovery fields after a successful run."""
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.coordinator.agent import build_coordinator_graph

    def make_llm_response(payload: dict[str, Any]) -> MagicMock:
        response = MagicMock()
        response.text = None
        response.content = json.dumps(payload)
        return response

    async def stub_skill_adapter_ok(runner: Any, input_data: dict, context: Any) -> SkillResult:
        return SkillResult(
            status="ok",
            result=f"完成:{context.skill_name}",
            artifact_refs=[],
            review={},
            budget={},
        )

    goal_response_payload = {
        "execution_mode": "dag",
        "reasoning": "需要先研究再输出下游结果",
        "goal_understanding": "跨领域任务",
    }
    decompose_response_payload = {
        "tasks": [
            {
                "id": "t1",
                "title": "研究量子计算",
                "description": "调研量子计算技术",
                "depends_on": [],
                "assigned_skill": "do_research",
                "input_data": {"query": "量子计算调研"},
            },
            {
                "id": "t2",
                "title": "撰写量子专利",
                "description": "基于研究结果撰写专利",
                "depends_on": ["t1"],
                "assigned_skill": "do_patent",
                "input_data": {"query": "量子专利"},
            },
        ],
        "reasoning": "先研究后撰写专利",
    }

    call_count = [0]

    def llm_factory(role: str, **kwargs: Any) -> AsyncMock:
        llm = AsyncMock()
        llm.ainvoke.return_value = make_llm_response(
            goal_response_payload if call_count[0] == 0 else decompose_response_payload
        )
        call_count[0] += 1
        return llm

    graph = build_coordinator_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "test-checkpoint-state-001"}}
    state_input = {
        "original_goal": "研究量子计算并撰写专利",
        "trace_id": "trace-checkpoint-state-001",
        "config": CoordinatorConfig(),
        "clarification_history": [],
    }

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter", side_effect=stub_skill_adapter_ok), \
         patch("agent.coordinator.agent.get_stream_writer", return_value=lambda _: None), \
         patch("agent.coordinator.executor.get_stream_writer", return_value=lambda _: None), \
         patch("core.models.get_llm", side_effect=llm_factory):
        mock_reg.skill_summary_for_llm.return_value = "技能摘要"
        mock_reg.list_skills.return_value = []
        mock_reg.is_registered.return_value = True
        mock_reg.get_runner.return_value = MagicMock()

        result = await graph.ainvoke(state_input, config=config)
        state_snapshot = await graph.aget_state(config)

    values = state_snapshot.values or {}
    assert result.get("completed_tasks"), "expected DAG execution to populate completed_tasks"
    for field in ("task_dag", "completed_tasks", "failed_tasks", "task_vars", "skill_runs"):
        assert field in values, f"checkpoint snapshot missing {field}: {sorted(values.keys())}"

    assert list(values["completed_tasks"].keys()) == ["t1", "t2"]
    assert list(values["task_vars"].keys()) == ["t1", "t2"]
    assert list(values["skill_runs"].keys()) == ["t1", "t2"]


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
