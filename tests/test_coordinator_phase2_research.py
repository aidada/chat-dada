from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from agent.coordinator.state import SkillContext, SkillResult
from agent.coordinator.skills import run_skill_via_adapter, _make_skill_interrupt_bridge


def _ctx(
    skill_name: str = "do_research",
    clarification_history: list[dict] | None = None,
    coordinator_task_id: str = "task-001",
    skill_invocation_id: str = "inv-001",
) -> SkillContext:
    return SkillContext(
        coordinator_task_id=coordinator_task_id,
        skill_invocation_id=skill_invocation_id,
        skill_name=skill_name,
        trace_id="trace-001",
        request_payload={"report_profile": "literature_review"},
        clarification_history=list(clarification_history or []),
    )


@pytest.mark.asyncio
async def test_research_adapter_ok_result_dict():
    async def runner(input_data: dict) -> dict:
        return {
            "result": "研究结果内容",
            "artifact_refs": [{"type": "file", "name": "report.md"}],
            "review": {"passed": True},
            "budget": {"cost": 0.5},
            "strategy": "parallel",
        }

    result = await run_skill_via_adapter(runner, {"query": "量子计算"}, _ctx())
    assert result.status == "ok"
    assert result.result == "研究结果内容"
    assert result.artifact_refs[0]["name"] == "report.md"
    assert result.review == {"passed": True}
    assert result.budget == {"cost": 0.5}
    assert result.strategy == "parallel"
    assert result.execution_time_seconds >= 0.0


@pytest.mark.asyncio
async def test_research_adapter_pydantic_model_result():
    from pydantic import BaseModel

    class FakeResult(BaseModel):
        result: str = ""
        artifact_refs: list[dict] = []
        review: dict = {}
        budget: dict = {}
        strategy: str = ""

    async def runner(input_data: dict) -> FakeResult:
        return FakeResult(result="pydantic result", strategy="sequential")

    result = await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert result.status == "ok"
    assert result.result == "pydantic result"
    assert result.strategy == "sequential"


@pytest.mark.asyncio
async def test_research_adapter_passes_clarification_history():
    received: dict[str, Any] = {}

    async def runner(input_data: dict) -> dict:
        received.update(input_data)
        return {"result": "ok", "artifact_refs": [], "review": {}, "budget": {}}

    history = [{"question": "研究方向?", "answer": "工程实现", "checkpoint_id": "cp_a"}]
    ctx = _ctx(clarification_history=history)
    await run_skill_via_adapter(runner, {"query": "ml研究"}, ctx)

    assert received.get("clarification_history") == history


@pytest.mark.asyncio
async def test_research_adapter_checkpoint_c_passthrough():
    """Checkpoint C accept entry in clarification_history reaches domain runner intact."""
    received: dict[str, Any] = {}
    checkpoint_c_q = "模块评审已通过。若还要继续微调，请说明；如无修改可忽略，系统将输出最终稿。"

    async def runner(input_data: dict) -> dict:
        received.update(input_data)
        return {"result": "快速恢复结论", "artifact_refs": [], "review": {}, "budget": {}}

    history = [
        {"question": "研究计划确认", "answer": "接受", "checkpoint_id": "cp_a"},
        {"question": checkpoint_c_q, "answer": "确认完成", "checkpoint_id": "cp_c"},
    ]
    ctx = _ctx(clarification_history=history)
    await run_skill_via_adapter(runner, {"query": "test"}, ctx)

    passed_history = received.get("clarification_history", [])
    assert len(passed_history) == 2
    assert passed_history[-1]["checkpoint_id"] == "cp_c"


@pytest.mark.asyncio
async def test_research_adapter_reraises_graph_interrupt():
    interrupt_obj = Interrupt(value={"content": "需要澄清研究方向"})

    async def interrupting_runner(input_data: dict) -> dict:
        raise GraphInterrupt((interrupt_obj,))

    with pytest.raises(GraphInterrupt):
        await run_skill_via_adapter(interrupting_runner, {"query": "test"}, _ctx())


@pytest.mark.asyncio
async def test_research_adapter_error_on_value_error():
    async def failing_runner(input_data: dict) -> dict:
        raise ValueError("LLM quota exceeded")

    result = await run_skill_via_adapter(failing_runner, {"query": "test"}, _ctx())
    assert result.status == "error"
    assert "LLM quota exceeded" in result.error


@pytest.mark.asyncio
async def test_research_adapter_sets_and_resets_interrupt_bridge():
    from agent.runtime.interaction import _graph_interrupt_bridge

    bridge_values_seen: list[Any] = []

    async def observing_runner(input_data: dict) -> dict:
        bridge_values_seen.append(_graph_interrupt_bridge.get(None))
        return {"result": "ok", "artifact_refs": [], "review": {}, "budget": {}}

    bridge_before = _graph_interrupt_bridge.get(None)
    await run_skill_via_adapter(observing_runner, {"query": "test"}, _ctx())
    bridge_after = _graph_interrupt_bridge.get(None)

    assert bridge_values_seen[0] is not None, "bridge must be set during execution"
    assert bridge_after == bridge_before, "bridge must be reset after execution"


@pytest.mark.asyncio
async def test_research_adapter_bridge_reset_on_interrupt():
    from agent.runtime.interaction import _graph_interrupt_bridge

    interrupt_obj = Interrupt(value={"content": "test"})

    async def interrupting_runner(input_data: dict) -> dict:
        raise GraphInterrupt((interrupt_obj,))

    bridge_before = _graph_interrupt_bridge.get(None)
    with pytest.raises(GraphInterrupt):
        await run_skill_via_adapter(interrupting_runner, {"query": "test"}, _ctx())
    assert _graph_interrupt_bridge.get(None) == bridge_before


@pytest.mark.asyncio
async def test_make_skill_interrupt_bridge_enriches_payload():
    captured: list[dict] = []
    with patch("agent.platform.interrupts.request_interrupt", side_effect=captured.append):
        bridge = _make_skill_interrupt_bridge("task-abc", "inv-xyz")
        bridge({"content": "需要用户输入"})

    assert captured[0]["coordinator_task_id"] == "task-abc"
    assert captured[0]["skill_invocation_id"] == "inv-xyz"
    assert captured[0]["interrupt_type"] == "human_input"
    assert captured[0]["content"] == "需要用户输入"
