from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from agent.coordinator.state import SkillContext, SkillResult
from agent.coordinator.skills import run_skill_via_adapter


def _ctx(
    clarification_history: list[dict] | None = None,
    report_profile: str = "",
) -> SkillContext:
    ctx = SkillContext(
        coordinator_task_id="task-patent-001",
        skill_invocation_id="inv-patent-001",
        skill_name="do_patent",
        trace_id="trace-patent",
        request_payload={"report_profile": report_profile},
        clarification_history=list(clarification_history or []),
    )
    return ctx


@pytest.mark.asyncio
async def test_patent_adapter_ok_result_dict():
    async def runner(input_data: dict) -> dict:
        assert input_data.get("query") == "发明技术", f"query not forwarded: {input_data}"
        return {
            "result": "专利权利要求书",
            "artifact_refs": [{"type": "file", "name": "patent.docx"}],
            "review": {"score": 0.9},
            "budget": {"cost": 1.2},
            "strategy": "",
        }

    result = await run_skill_via_adapter(runner, {"query": "发明技术"}, _ctx())
    assert result.status == "ok"
    assert result.result == "专利权利要求书"
    assert result.artifact_refs[0]["name"] == "patent.docx"
    assert result.review == {"score": 0.9}
    assert result.budget == {"cost": 1.2}


@pytest.mark.asyncio
async def test_patent_adapter_ok_pydantic_result():
    from pydantic import BaseModel

    class PatentDomainResult(BaseModel):
        result: str = ""
        artifact_refs: list[dict] = []
        review: dict = {}
        budget: dict = {}
        strategy: str = ""

    async def runner(input_data: dict) -> PatentDomainResult:
        return PatentDomainResult(
            result="专利权利要求书",
            artifact_refs=[{"type": "file", "name": "patent.docx"}],
            review={"score": 0.9},
            budget={"cost": 1.2},
        )

    result = await run_skill_via_adapter(runner, {"query": "发明技术"}, _ctx())
    assert result.status == "ok"
    assert result.artifact_refs[0]["name"] == "patent.docx"


@pytest.mark.asyncio
async def test_patent_adapter_forwards_report_profile():
    received: dict[str, Any] = {}

    async def runner(input_data: dict) -> dict:
        received.update(input_data)
        return {"result": "ok", "artifact_refs": [], "review": {}, "budget": {}}

    ctx = _ctx(report_profile="utility_patent")
    await run_skill_via_adapter(runner, {"query": "发明"}, ctx)
    assert received.get("report_profile") == "utility_patent"


@pytest.mark.asyncio
async def test_patent_adapter_forwards_clarification_history():
    received: dict[str, Any] = {}

    async def runner(input_data: dict) -> dict:
        received.update(input_data)
        return {"result": "专利", "artifact_refs": [], "review": {}, "budget": {}}

    history = [{"question": "主权项方向?", "answer": "方法权利要求"}]
    ctx = _ctx(clarification_history=history)
    await run_skill_via_adapter(runner, {"query": "发明技术"}, ctx)
    assert received.get("clarification_history") == history


@pytest.mark.asyncio
async def test_patent_adapter_reraises_graph_interrupt():
    interrupt_obj = Interrupt(value={"content": "请描述核心创新点"})

    async def runner(input_data: dict) -> dict:
        raise GraphInterrupt((interrupt_obj,))

    ctx = _ctx()
    with pytest.raises(GraphInterrupt):
        await run_skill_via_adapter(runner, {"query": "test"}, ctx)


@pytest.mark.asyncio
async def test_patent_adapter_error_on_runtime_error():
    async def runner(input_data: dict) -> dict:
        raise RuntimeError("Patent LLM timeout")

    ctx = _ctx()
    result = await run_skill_via_adapter(runner, {"query": "test"}, ctx)
    assert result.status == "error"
    assert "Patent LLM timeout" in (result.error or "")


@pytest.mark.asyncio
async def test_patent_adapter_bridge_reset_after_execution():
    from agent.runtime.interaction import _graph_interrupt_bridge

    async def runner(input_data: dict) -> dict:
        return {"result": "专利", "artifact_refs": [], "review": {}, "budget": {}}

    bridge_before = _graph_interrupt_bridge.get(None)
    await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert _graph_interrupt_bridge.get(None) == bridge_before


@pytest.mark.asyncio
async def test_patent_adapter_bridge_reset_on_interrupt():
    from agent.runtime.interaction import _graph_interrupt_bridge

    interrupt_obj = Interrupt(value={"content": "test"})

    async def runner(input_data: dict) -> dict:
        raise GraphInterrupt((interrupt_obj,))

    bridge_before = _graph_interrupt_bridge.get(None)
    with pytest.raises(GraphInterrupt):
        await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert _graph_interrupt_bridge.get(None) == bridge_before
