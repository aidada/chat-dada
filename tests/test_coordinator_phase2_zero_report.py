from __future__ import annotations

from typing import Any

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from agent.coordinator.state import SkillContext, SkillResult
from agent.coordinator.skills import run_skill_via_adapter


def _ctx(
    clarification_history: list[dict] | None = None,
) -> SkillContext:
    return SkillContext(
        coordinator_task_id="task-zero-001",
        skill_invocation_id="inv-zero-001",
        skill_name="do_zero_report",
        trace_id="trace-zero",
        request_payload={},
        clarification_history=list(clarification_history or []),
    )


@pytest.mark.asyncio
async def test_zero_report_adapter_ok_result_dict():
    async def runner(input_data: dict) -> dict:
        return {
            "result": "事故分析报告正文",
            "artifact_refs": [{"type": "file", "name": "zero_report.md"}],
            "review": {"root_cause": "配置错误", "passed": True},
            "budget": {"cost": 0.8},
            "strategy": "planning",
        }

    result = await run_skill_via_adapter(runner, {"query": "2024-01线上故障"}, _ctx())
    assert result.status == "ok"
    assert "分析报告" in result.result
    assert result.artifact_refs[0]["name"] == "zero_report.md"
    assert result.review["root_cause"] == "配置错误"
    assert result.budget["cost"] == 0.8
    assert result.strategy == "planning"


@pytest.mark.asyncio
async def test_zero_report_review_full_fields_propagated():
    """All review sub-fields must survive the extraction chain."""
    async def runner(input_data: dict) -> dict:
        return {
            "result": "报告",
            "artifact_refs": [],
            "review": {
                "passed": True,
                "score": 0.92,
                "issues": [],
                "corrective_actions": ["整改1", "整改2"],
                "timeline": "事故时间线",
            },
            "budget": {"total_cost_usd": 1.2, "llm_calls": 18},
            "strategy": "",
        }

    result = await run_skill_via_adapter(runner, {"query": "宕机事故"}, _ctx())
    assert result.review["score"] == 0.92
    assert result.review["corrective_actions"] == ["整改1", "整改2"]
    assert result.budget["total_cost_usd"] == 1.2


@pytest.mark.asyncio
async def test_zero_report_adapter_ok_pydantic_result():
    from pydantic import BaseModel

    class ZeroReportResult(BaseModel):
        result: str = ""
        artifact_refs: list[dict] = []
        review: dict = {}
        budget: dict = {}
        strategy: str = ""

    async def runner(input_data: dict) -> ZeroReportResult:
        return ZeroReportResult(
            result="事故分析报告正文",
            artifact_refs=[{"name": "zero_report.md"}],
            review={"root_cause": "配置错误"},
        )

    result = await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert result.status == "ok"
    assert result.review["root_cause"] == "配置错误"


@pytest.mark.asyncio
async def test_zero_report_adapter_clarification_history_forwarded():
    received: dict[str, Any] = {}

    async def runner(input_data: dict) -> dict:
        received.update(input_data)
        return {"result": "ok", "artifact_refs": [], "review": {}, "budget": {}}

    history = [{"question": "故障影响范围?", "answer": "全量用户"}]
    ctx = _ctx(clarification_history=history)
    await run_skill_via_adapter(runner, {"query": "服务器宕机"}, ctx)
    assert received.get("clarification_history") == history


@pytest.mark.asyncio
async def test_zero_report_adapter_reraises_graph_interrupt():
    interrupt_obj = Interrupt(value={"content": "请提供故障时间线详情"})

    async def runner(input_data: dict) -> dict:
        raise GraphInterrupt((interrupt_obj,))

    with pytest.raises(GraphInterrupt):
        await run_skill_via_adapter(runner, {"query": "test"}, _ctx())


@pytest.mark.asyncio
async def test_zero_report_adapter_error_on_runtime_error():
    async def runner(input_data: dict) -> dict:
        raise RuntimeError("Analysis pipeline failed")

    result = await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert result.status == "error"
    assert "Analysis pipeline failed" in (result.error or "")


@pytest.mark.asyncio
async def test_zero_report_adapter_bridge_cleanup():
    from agent.runtime.interaction import _graph_interrupt_bridge

    async def runner(input_data: dict) -> dict:
        return {"result": "ok", "artifact_refs": [], "review": {}, "budget": {}}

    bridge_before = _graph_interrupt_bridge.get(None)
    await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert _graph_interrupt_bridge.get(None) == bridge_before


@pytest.mark.asyncio
async def test_zero_report_adapter_bridge_cleanup_on_interrupt():
    from agent.runtime.interaction import _graph_interrupt_bridge

    interrupt_obj = Interrupt(value={"content": "test"})

    async def runner(input_data: dict) -> dict:
        raise GraphInterrupt((interrupt_obj,))

    bridge_before = _graph_interrupt_bridge.get(None)
    with pytest.raises(GraphInterrupt):
        await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert _graph_interrupt_bridge.get(None) == bridge_before
