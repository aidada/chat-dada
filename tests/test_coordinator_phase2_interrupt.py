# tests/test_coordinator_phase2_interrupt.py
"""Phase 2 interrupt/resume tests — bug fixes and full cycle verification."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from agent.coordinator.state import CoordinatorConfig, CoordinatorState, SkillContext, Task


# ── Bug Fix 1: interrupt_state must have skill_invocation_id (PRD §6.4) ──────

@pytest.mark.asyncio
async def test_execute_tasks_interrupt_state_has_skill_invocation_id():
    """
    BEFORE FIX: interrupt_state = {"interrupted_by": "skill_interrupt"}
    AFTER FIX:  interrupt_state includes skill_invocation_id, coordinator_task_id,
                task_id, skill
    """
    task = Task(id="t1", title="研究任务", assigned_skill="do_research",
                input_data={"query": "test"}, depends_on=[])

    async def interrupting_runner(input_data: dict) -> dict:
        raise GraphInterrupt((Interrupt(value={"content": "请确认研究方向"}),))

    state: CoordinatorState = {
        "task_dag": [task],
        "pending_tasks": ["t1"],
        "completed_tasks": {},
        "failed_tasks": {},
        "running_tasks": {},
        "task_vars": {},
        "skill_runs": {},
        "trace_id": "trace-interrupt-bug-001",
        "config": CoordinatorConfig(),
        "clarification_history": [],
    }

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.executor.get_stream_writer", return_value=lambda _: None):
        mock_reg.get_runner.return_value = interrupting_runner
        from agent.coordinator.executor import execute_tasks_node
        result = await execute_tasks_node(state)

    interrupt_state = result.get("interrupt_state")
    assert interrupt_state is not None
    assert "skill_invocation_id" in interrupt_state, (
        f"PRD §6.4: interrupt_state missing skill_invocation_id. Got: {interrupt_state}"
    )
    assert interrupt_state["skill_invocation_id"] != ""
    assert "coordinator_task_id" in interrupt_state
    assert interrupt_state.get("task_id") == "t1"
    assert interrupt_state.get("skill") == "do_research"


@pytest.mark.asyncio
async def test_execute_tasks_interrupt_state_coordinator_task_id_matches_trace():
    task = Task(id="t1", title="T", assigned_skill="do_research",
                input_data={"query": "x"}, depends_on=[])

    async def interrupting_runner(input_data: dict) -> dict:
        raise GraphInterrupt((Interrupt(value={"content": "?"}),))

    state: CoordinatorState = {
        "task_dag": [task], "pending_tasks": ["t1"], "completed_tasks": {},
        "failed_tasks": {}, "running_tasks": {}, "task_vars": {}, "skill_runs": {},
        "trace_id": "specific-trace-xyz", "config": CoordinatorConfig(), "clarification_history": [],
    }

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.executor.get_stream_writer", return_value=lambda _: None):
        mock_reg.get_runner.return_value = interrupting_runner
        from agent.coordinator.executor import execute_tasks_node
        result = await execute_tasks_node(state)

    assert result["interrupt_state"]["coordinator_task_id"] == "specific-trace-xyz"


# ── Bug Fix 2: dead code removed from execute_single_skill_node ──────────────

@pytest.mark.asyncio
async def test_execute_single_skill_graph_interrupt_propagates_unchanged():
    """
    After removing dead code, GraphInterrupt must escape execute_single_skill_node.
    """
    async def interrupting_runner(input_data: dict) -> dict:
        raise GraphInterrupt((Interrupt(value={"content": "澄清问题"}),))

    state: CoordinatorState = {
        "selected_skill": "do_research",
        "skill_input": {"query": "量子计算"},
        "original_goal": "量子计算研究",
        "trace_id": "trace-single-bug-001",
        "clarification_history": [],
    }

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.agent.get_stream_writer", return_value=lambda _: None):
        mock_reg.get_runner.return_value = interrupting_runner
        from agent.coordinator.agent import execute_single_skill_node
        with pytest.raises(GraphInterrupt):
            await execute_single_skill_node(state)


# ── Preloaded replies mechanism ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_preloaded_replies_feeds_ask_user_in_order():
    from agent.runtime.interaction import (
        ask_user, reset_preloaded_user_replies, set_preloaded_user_replies,
    )
    token = set_preloaded_user_replies(["工程实现", "三年"])
    try:
        a1 = await ask_user("研究方向?")
        a2 = await ask_user("时间范围?")
        a3 = await ask_user("还有别的?")  # queue exhausted
    finally:
        reset_preloaded_user_replies(token)

    assert a1 == "工程实现"
    assert a2 == "三年"
    assert a3 is None


@pytest.mark.asyncio
async def test_interrupt_bridge_full_cycle_enriches_payload():
    """Full cycle: ask_user() → bridge → request_interrupt → enriched payload."""
    from agent.coordinator.skills import run_skill_via_adapter

    interrupt_payloads: list[dict] = []

    def mock_request_interrupt(payload: dict) -> None:
        interrupt_payloads.append(payload)
        raise GraphInterrupt((Interrupt(value=payload),))

    async def runner_that_asks(input_data: dict) -> dict:
        from agent.runtime.interaction import ask_user
        await ask_user("研究方向是什么?", context="影响报告结构")
        return {"result": "completed", "artifact_refs": [], "review": {}, "budget": {}}

    ctx = SkillContext(
        coordinator_task_id="task-bridge-test",
        skill_invocation_id="inv-bridge-001",
        skill_name="do_research",
        trace_id="trace-bridge",
        request_payload={},
        clarification_history=[],
    )

    with patch("agent.platform.interrupts.request_interrupt", side_effect=mock_request_interrupt):
        with pytest.raises(GraphInterrupt):
            await run_skill_via_adapter(runner_that_asks, {"query": "test"}, ctx)

    assert len(interrupt_payloads) == 1
    p = interrupt_payloads[0]
    assert p["coordinator_task_id"] == "task-bridge-test"
    assert p["skill_invocation_id"] == "inv-bridge-001"
    assert p["interrupt_type"] == "human_input"


@pytest.mark.asyncio
async def test_preloaded_replies_bypass_bridge_during_resume():
    """On resume with preloaded replies, ask_user() returns reply without bridging."""
    from agent.coordinator.skills import run_skill_via_adapter
    from agent.runtime.interaction import (
        reset_preloaded_user_replies, set_preloaded_user_replies,
    )

    bridge_called = [False]

    async def runner_that_asks(input_data: dict) -> dict:
        from agent.runtime.interaction import ask_user
        answer = await ask_user("研究方向是什么?", context="test")
        return {"result": f"方向={answer}", "artifact_refs": [], "review": {}, "budget": {}}

    ctx = SkillContext(
        coordinator_task_id="task-resume-001",
        skill_invocation_id="inv-resume-001",
        skill_name="do_research",
        trace_id="trace",
        request_payload={},
        clarification_history=[{"question": "研究方向是什么?", "answer": "工程实现"}],
    )

    def side_effect_bridge(p):
        bridge_called[0] = True
        raise GraphInterrupt((Interrupt(value=p),))

    token = set_preloaded_user_replies(["工程实现"])
    try:
        with patch("agent.platform.interrupts.request_interrupt", side_effect=side_effect_bridge):
            result = await run_skill_via_adapter(runner_that_asks, {"query": "test"}, ctx)
    finally:
        reset_preloaded_user_replies(token)

    assert result.status == "ok"
    assert "工程实现" in result.result
    assert not bridge_called[0], "Bridge must not be called when preloaded reply is available"
