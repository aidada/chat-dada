from __future__ import annotations

from typing import Any

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from agent.coordinator.state import SkillContext, SkillResult
from agent.coordinator.skills import run_skill_via_adapter


def _ctx() -> SkillContext:
    return SkillContext(
        coordinator_task_id="task-ppt-001",
        skill_invocation_id="inv-ppt-001",
        skill_name="do_ppt",
        trace_id="trace-ppt",
        request_payload={},
        clarification_history=[],
    )


@pytest.mark.asyncio
async def test_ppt_adapter_ok_result_with_pptx_artifact():
    async def runner(input_data: dict) -> dict:
        return {
            "result": "PPT已生成：slides.pptx (12 slides)",
            "artifact_refs": [{"type": "file", "name": "slides.pptx", "url": "/outputs/slides.pptx"}],
            "review": {},
            "budget": {"cost": 0.3},
            "strategy": "",
        }

    result = await run_skill_via_adapter(runner, {"query": "AI汇报材料"}, _ctx())
    assert result.status == "ok"
    assert "PPT" in result.result
    assert result.artifact_refs[0]["name"] == "slides.pptx"
    assert result.artifact_refs[0]["url"] == "/outputs/slides.pptx"


@pytest.mark.asyncio
async def test_ppt_adapter_ok_pydantic_result():
    from pydantic import BaseModel

    class PptDomainResult(BaseModel):
        result: str = ""
        artifact_refs: list[dict] = []
        review: dict = {}
        budget: dict = {}
        strategy: str = ""

    async def runner(input_data: dict) -> PptDomainResult:
        return PptDomainResult(
            result="PPT已生成：report.pptx",
            artifact_refs=[{"name": "report.pptx"}],
        )

    result = await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert result.status == "ok"
    assert result.artifact_refs[0]["name"] == "report.pptx"


@pytest.mark.asyncio
async def test_ppt_adapter_query_forwarded():
    received: dict[str, Any] = {}

    async def runner(input_data: dict) -> dict:
        received.update(input_data)
        return {"result": "ok", "artifact_refs": [], "review": {}, "budget": {}}

    await run_skill_via_adapter(runner, {"query": "PPT主题"}, _ctx())
    assert received.get("query") == "PPT主题"


@pytest.mark.asyncio
async def test_ppt_adapter_error_on_connection_error():
    async def runner(input_data: dict) -> dict:
        raise ConnectionError("OfficeCLI server unavailable")

    result = await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert result.status == "error"
    assert "OfficeCLI" in (result.error or "")


@pytest.mark.asyncio
async def test_ppt_adapter_error_on_os_error():
    async def runner(input_data: dict) -> dict:
        raise OSError("File write failed")

    result = await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert result.status == "error"
    assert result.error is not None


@pytest.mark.asyncio
async def test_ppt_adapter_bridge_cleanup_on_error():
    """Bridge is reset even when OfficeCLI raises non-interrupt error."""
    from agent.runtime.interaction import _graph_interrupt_bridge

    async def runner(input_data: dict) -> dict:
        raise OSError("File write failed")

    bridge_before = _graph_interrupt_bridge.get(None)
    result = await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert result.status == "error"
    assert _graph_interrupt_bridge.get(None) == bridge_before


@pytest.mark.asyncio
async def test_ppt_adapter_bridge_cleanup_on_success():
    from agent.runtime.interaction import _graph_interrupt_bridge

    async def runner(input_data: dict) -> dict:
        return {"result": "PPT done", "artifact_refs": [], "review": {}, "budget": {}}

    bridge_before = _graph_interrupt_bridge.get(None)
    await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
    assert _graph_interrupt_bridge.get(None) == bridge_before


@pytest.mark.asyncio
async def test_ppt_adapter_reraises_graph_interrupt():
    interrupt_obj = Interrupt(value={"content": "请确认PPT风格"})

    async def runner(input_data: dict) -> dict:
        raise GraphInterrupt((interrupt_obj,))

    with pytest.raises(GraphInterrupt):
        await run_skill_via_adapter(runner, {"query": "test"}, _ctx())
