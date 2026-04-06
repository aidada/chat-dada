"""
Phase 2 End-to-End tests — full coordinator_graph.ainvoke() cycles for:
  1. single_skill mode (do_research)
  2. DAG mode (research → patent with upstream context)
  3. DAG failure propagation (STOP_DEPENDENTS)
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.coordinator.state import (
    CoordinatorConfig,
    DAGFailureStrategy,
    ExecutionMode,
    SkillResult,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_llm_response(payload: dict) -> Any:
    """Build a fake LLM response object whose text is the JSON-encoded payload.

    response_text() in core/models.py checks .text first, then .content.
    We must set .text = None so response_text falls through to .content.
    """
    mock_response = MagicMock()
    mock_response.text = None  # force response_text to use .content
    mock_response.content = json.dumps(payload)
    return mock_response


def _make_mock_llm(payload: dict) -> AsyncMock:
    """Return an AsyncMock LLM whose ainvoke() returns a fake response."""
    llm = AsyncMock()
    llm.ainvoke.return_value = _make_llm_response(payload)
    return llm


async def _stub_skill_adapter_ok(runner: Any, input_data: dict, context: Any) -> SkillResult:
    """Stub for run_skill_via_adapter: returns appropriate SkillResult per skill_name."""
    skill = context.skill_name
    if skill == "do_research":
        return SkillResult(
            status="ok",
            result="研究结果: " + input_data.get("query", ""),
            artifact_refs=[{"name": "research_report.md", "type": "file"}],
            review={"passed": True},
            budget={"cost": 2.0},
        )
    elif skill == "do_patent":
        return SkillResult(
            status="ok",
            result="专利草案",
            artifact_refs=[{"name": "patent.docx", "type": "file"}],
            review={"score": 0.85},
            budget={"cost": 1.5},
        )
    raise ValueError(f"Unknown skill: {skill}")


async def _stub_skill_adapter_research_fail(runner: Any, input_data: dict, context: Any) -> SkillResult:
    """Stub that makes do_research fail and do_patent succeed."""
    skill = context.skill_name
    if skill == "do_research":
        raise RuntimeError("研究服务不可用")
    elif skill == "do_patent":
        return SkillResult(
            status="ok",
            result="专利草案",
            artifact_refs=[{"name": "patent.docx", "type": "file"}],
            review={"score": 0.85},
            budget={"cost": 1.5},
        )
    raise ValueError(f"Unknown skill: {skill}")


def _build_graph():
    """Build coordinator graph fresh for each test."""
    from agent.coordinator.agent import build_coordinator_graph
    return build_coordinator_graph()


# ── Test 1: single_skill mode (do_research) ───────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_single_skill_research():
    """
    Full graph invocation in single_skill mode.
    LLM returns execution_mode=single_skill, selected_skill=do_research.
    run_skill_via_adapter is patched to return a pre-built SkillResult.
    """
    goal_response_payload = {
        "execution_mode": "single_skill",
        "reasoning": "明确的研究任务",
        "goal_understanding": "单技能任务",
        "selected_skill": "do_research",
        "skill_input": {"query": "研究量子计算"},
    }

    mock_llm = _make_mock_llm(goal_response_payload)

    # skill_registry must have do_research registered so get_runner returns non-None
    mock_runner = MagicMock()

    graph = _build_graph()

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter",
               side_effect=_stub_skill_adapter_ok), \
         patch("agent.coordinator.agent.get_stream_writer",
               return_value=lambda _: None), \
         patch("agent.coordinator.executor.get_stream_writer",
               return_value=lambda _: None), \
         patch("core.models.get_llm", return_value=mock_llm):

        mock_reg.skill_summary_for_llm.return_value = "技能摘要"
        mock_reg.list_skills.return_value = []
        mock_reg.is_registered.return_value = True
        mock_reg.get_runner.return_value = mock_runner

        config = {"configurable": {"thread_id": "test-single-skill-001"}}
        state_input = {
            "original_goal": "研究量子计算",
            "trace_id": "trace-e2e-single-001",
            "config": CoordinatorConfig(),
            "clarification_history": [],
        }

        result = await graph.ainvoke(state_input, config=config)

    # Assertions
    assert result.get("execution_mode") == ExecutionMode.SINGLE_SKILL, (
        f"Expected SINGLE_SKILL, got {result.get('execution_mode')}"
    )
    final_result = result.get("final_result") or ""
    assert "研究结果" in final_result, (
        f"Expected '研究结果' in final_result, got: {final_result!r}"
    )
    artifact_refs = result.get("artifact_refs") or []
    assert len(artifact_refs) > 0, "Expected at least one artifact_ref"
    assert artifact_refs[0]["name"] == "research_report.md", (
        f"Expected 'research_report.md', got {artifact_refs[0]}"
    )
    review = result.get("review") or {}
    assert review == {"passed": True}, f"Expected review={{'passed': True}}, got {review}"


# ── Test 2: DAG mode (research → patent) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_dag_research_then_patent():
    """
    Full DAG graph: t1(do_research) → t2(do_patent depends_on t1).
    LLM understand_goal returns dag mode.
    _call_llm_json (decompose_tasks) returns 2-task DAG.
    run_skill_via_adapter returns appropriate SkillResult per skill.
    Asserts both tasks completed, t1 result in task_vars, artifact_refs merged.
    """
    # understand_goal returns dag mode
    goal_response_payload = {
        "execution_mode": "dag",
        "reasoning": "需要研究后撰写专利",
        "goal_understanding": "跨领域任务",
    }

    # decompose_tasks returns 2-task DAG
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

    mock_runner = MagicMock()

    # We need to make the LLM return different responses depending on call order
    # understand_goal calls get_llm("orchestrator")
    # decompose_tasks calls _call_llm_json which also calls get_llm("orchestrator")
    call_count = [0]

    def llm_factory(role: str, **kwargs: Any):
        """Return different mock LLMs for understand_goal vs decompose_tasks calls."""
        llm = AsyncMock()
        if call_count[0] == 0:
            llm.ainvoke.return_value = _make_llm_response(goal_response_payload)
        else:
            llm.ainvoke.return_value = _make_llm_response(decompose_response_payload)
        call_count[0] += 1
        return llm

    graph = _build_graph()

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter",
               side_effect=_stub_skill_adapter_ok), \
         patch("agent.coordinator.agent.get_stream_writer",
               return_value=lambda _: None), \
         patch("agent.coordinator.executor.get_stream_writer",
               return_value=lambda _: None), \
         patch("core.models.get_llm", side_effect=llm_factory):

        mock_reg.skill_summary_for_llm.return_value = "技能摘要"
        mock_reg.list_skills.return_value = []
        mock_reg.is_registered.return_value = True
        mock_reg.get_runner.return_value = mock_runner

        config = {"configurable": {"thread_id": "test-dag-research-patent-001"}}
        state_input = {
            "original_goal": "研究量子计算并撰写专利",
            "trace_id": "trace-e2e-dag-001",
            "config": CoordinatorConfig(),
            "clarification_history": [],
        }

        result = await graph.ainvoke(state_input, config=config)

    # Both tasks must be completed
    completed = result.get("completed_tasks") or {}
    assert "t1" in completed, f"t1 not in completed_tasks: {list(completed.keys())}"
    assert "t2" in completed, f"t2 not in completed_tasks: {list(completed.keys())}"

    # t1 result should be in task_vars
    task_vars = result.get("task_vars") or {}
    assert "t1" in task_vars, f"t1 not in task_vars: {list(task_vars.keys())}"
    t1_var = task_vars["t1"]
    assert "研究结果" in t1_var.summary, (
        f"Expected '研究结果' in t1 task_var summary, got: {t1_var.summary!r}"
    )

    # Artifact refs should include both tasks' artifacts
    artifact_refs = result.get("artifact_refs") or []
    artifact_names = [ref["name"] for ref in artifact_refs]
    assert "research_report.md" in artifact_names, (
        f"research_report.md not in artifact_refs: {artifact_names}"
    )
    assert "patent.docx" in artifact_names, (
        f"patent.docx not in artifact_refs: {artifact_names}"
    )

    # final_result should contain patent draft (t2 is the final task, depends on t1)
    final_result = result.get("final_result") or ""
    assert len(final_result) > 0, "final_result should not be empty"


# ── Test 3: DAG failure propagation (STOP_DEPENDENTS) ────────────────────────

@pytest.mark.asyncio
async def test_e2e_dag_failure_propagation_stop_dependents():
    """
    DAG mode: t1(do_research) fails with RuntimeError.
    With STOP_DEPENDENTS strategy, t2(do_patent, depends_on t1) must be cancelled.
    Asserts: t1 in failed_tasks, t2 NOT in completed_tasks, final_result is non-empty error.
    """
    goal_response_payload = {
        "execution_mode": "dag",
        "reasoning": "需要研究后撰写专利",
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

    mock_runner = MagicMock()
    call_count = [0]

    def llm_factory(role: str, **kwargs: Any):
        llm = AsyncMock()
        if call_count[0] == 0:
            llm.ainvoke.return_value = _make_llm_response(goal_response_payload)
        else:
            llm.ainvoke.return_value = _make_llm_response(decompose_response_payload)
        call_count[0] += 1
        return llm

    graph = _build_graph()

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter",
               side_effect=_stub_skill_adapter_research_fail), \
         patch("agent.coordinator.agent.get_stream_writer",
               return_value=lambda _: None), \
         patch("agent.coordinator.executor.get_stream_writer",
               return_value=lambda _: None), \
         patch("core.models.get_llm", side_effect=llm_factory):

        mock_reg.skill_summary_for_llm.return_value = "技能摘要"
        mock_reg.list_skills.return_value = []
        mock_reg.is_registered.return_value = True
        mock_reg.get_runner.return_value = mock_runner

        config_obj = CoordinatorConfig(
            failure_strategy=DAGFailureStrategy.STOP_DEPENDENTS
        )
        config = {"configurable": {"thread_id": "test-dag-failure-001"}}
        state_input = {
            "original_goal": "研究量子计算并撰写专利",
            "trace_id": "trace-e2e-failure-001",
            "config": config_obj,
            "clarification_history": [],
        }

        result = await graph.ainvoke(state_input, config=config)

    # t1 must be in failed_tasks
    failed_tasks = result.get("failed_tasks") or {}
    assert "t1" in failed_tasks, (
        f"t1 should be in failed_tasks. Got failed_tasks keys: {list(failed_tasks.keys())}"
    )

    # t2 must NOT be in completed_tasks (cancelled or never run)
    completed_tasks = result.get("completed_tasks") or {}
    assert "t2" not in completed_tasks, (
        f"t2 should not be in completed_tasks. completed_tasks: {list(completed_tasks.keys())}"
    )

    # final_result must be a non-empty error message
    final_result = result.get("final_result") or ""
    assert len(final_result) > 0, "final_result should be non-empty error message"
    # The error message should mention the failure
    assert "失败" in final_result or "error" in final_result.lower() or "t1" in final_result, (
        f"final_result should contain failure info, got: {final_result!r}"
    )
