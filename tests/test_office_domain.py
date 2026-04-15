from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


def test_infer_default_create_file_from_user_intent() -> None:
    from agent.domains.office.workflow import _infer_default_create_file

    filename = _infer_default_create_file(
        "帮我在下载文件夹创建一个 PPT，大概 3 页，内容是介绍 chat-dada这个agent 软件 可以帮助你完成什么工作，解决的痛点是什么",
        "",
        "pptx",
    )

    assert filename == "chat-dada-agent-intro.pptx"


def test_infer_default_create_file_preserves_explicit_filename() -> None:
    from agent.domains.office.workflow import _infer_default_create_file

    filename = _infer_default_create_file(
        "请创建 quarterly-review.pptx，并放到下载文件夹",
        "",
        "pptx",
    )

    assert filename == "quarterly-review.pptx"


@pytest.mark.asyncio
async def test_office_domain_server_artifact_falls_back_to_outputs_snapshot(tmp_path) -> None:
    from agent.domains.office.orchestrated import run_office_domain_orchestrated

    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    output_file = outputs_dir / "deck.pptx"

    async def fake_stream(*_args, **_kwargs):
        output_file.touch()
        return {
            "final_result": """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[],"stats":{}}
```""",
            "step_history": [{"strategy": "sequential"}],
        }

    with (
        patch("agent.domains.office.orchestrated.stream_nested_graph", new=AsyncMock(side_effect=fake_stream)),
        patch("agent.domains.office.orchestrated.ALLOWED_DIR", outputs_dir),
        patch("agent.domains.office.orchestrated.infer_office_runtime_target", return_value="server"),
        patch(
            "agent.domains.office.orchestrated.execute_officecli_spec",
            new=AsyncMock(return_value={"success": True, "message": "Closing resident.", "command": "officecli close deck.pptx"}),
        ),
    ):
        result = await run_office_domain_orchestrated({"query": "做一个 PPT", "task_id": "office_server"})

    assert result.status == "ok"
    assert result.review["passed"] is True
    assert result.artifact_refs[0]["location"] == "server"
    assert result.artifact_refs[0]["url"] == "/download/deck.pptx"


@pytest.mark.asyncio
async def test_office_domain_desktop_artifact_uses_returned_local_path() -> None:
    from agent.domains.office.orchestrated import run_office_domain_orchestrated

    payload = """```json
{"operation":"edit","validated":true,"summary":"updated","artifacts":[{"filename":"deck.pptx","path":"/Users/test/Desktop/deck.pptx","format":"pptx","role":"primary"}],"stats":{}}
```"""

    with (
        patch(
            "agent.domains.office.orchestrated.stream_nested_graph",
            new=AsyncMock(return_value={"final_result": payload, "step_history": [{"strategy": "sequential"}]}),
        ),
        patch("agent.domains.office.orchestrated.infer_office_runtime_target", return_value="desktop"),
        patch(
            "agent.domains.office.orchestrated.execute_officecli_spec",
            new=AsyncMock(return_value={"success": True, "message": "Closing resident.", "command": "officecli close /Users/test/Desktop/deck.pptx"}),
        ),
    ):
        result = await run_office_domain_orchestrated({"query": "修改这个 PPT", "task_id": "office_desktop"})

    assert result.status == "ok"
    assert result.artifact_refs[0]["location"] == "desktop"
    assert result.artifact_refs[0]["path"] == "/Users/test/Desktop/deck.pptx"
    assert "url" not in result.artifact_refs[0]


@pytest.mark.asyncio
async def test_office_domain_inspect_allows_no_artifact() -> None:
    from agent.domains.office.orchestrated import run_office_domain_orchestrated

    payload = """```json
{"operation":"inspect","validated":false,"summary":"已完成检查","artifacts":[],"stats":{}}
```"""

    with (
        patch(
            "agent.domains.office.orchestrated.stream_nested_graph",
            new=AsyncMock(return_value={"final_result": payload, "step_history": [{"strategy": "sequential"}]}),
        ),
        patch("agent.domains.office.orchestrated.infer_office_runtime_target", return_value="server"),
    ):
        result = await run_office_domain_orchestrated({"query": "检查这个文档", "task_id": "office_inspect"})

    assert result.status == "ok"
    assert result.review["passed"] is True
    assert result.artifact_refs == []


@pytest.mark.asyncio
async def test_office_domain_write_close_failure_marks_error() -> None:
    from agent.domains.office.orchestrated import run_office_domain_orchestrated

    payload = """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"deck.pptx","path":"/Users/test/Desktop/deck.pptx","format":"pptx","role":"primary"}],"stats":{}}
```"""

    with (
        patch(
            "agent.domains.office.orchestrated.stream_nested_graph",
            new=AsyncMock(return_value={"final_result": payload, "step_history": [{"strategy": "sequential"}]}),
        ),
        patch("agent.domains.office.orchestrated.infer_office_runtime_target", return_value="desktop"),
        patch(
            "agent.domains.office.orchestrated.execute_officecli_spec",
            new=AsyncMock(
                return_value={
                    "success": False,
                    "message": 'OfficeCLI close requires a non-empty "file" parameter.',
                    "command": "officecli close deck.pptx",
                }
            ),
        ),
    ):
        result = await run_office_domain_orchestrated({"query": "做一个 PPT", "task_id": "office_close_fail"})

    assert result.status == "error"
    assert result.review["passed"] is False
    assert result.review["reason"] == "Office close/flush failed"
    assert result.artifact_refs[0]["path"] == "/Users/test/Desktop/deck.pptx"


@pytest.mark.asyncio
async def test_ppt_wrapper_maps_ppt_artifact_type() -> None:
    from agent.domains.office.orchestrated import OfficeDomainResult
    from agent.domains.ppt.orchestrated import run_ppt_domain_orchestrated

    with patch(
        "agent.domains.ppt.orchestrated.run_office_domain_orchestrated",
        new=AsyncMock(
            return_value=OfficeDomainResult(
                status="ok",
                result="done",
                artifact_refs=[{"name": "deck.pptx", "type": "file", "format": "pptx"}],
                review={"passed": True},
                budget={},
            )
        ),
    ):
        result = await run_ppt_domain_orchestrated({"query": "做一个 PPT"})

    assert result.artifact_refs[0]["type"] == "pptx"


@pytest.mark.asyncio
async def test_office_workflow_ppt_quality_stats_required_for_create() -> None:
    from agent.domains.office.workflow import evaluate_node

    state = {
        "format": "pptx",
        "operation": "create",
        "write_required": True,
        "intermediate_results": [
            {
                "output": """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"deck.pptx","format":"pptx","role":"primary"}],"stats":{}}
```"""
            }
        ],
    }

    result = await evaluate_node(state)

    assert result["evaluations"][0]["passed"] is False
    assert any("质量 stats" in issue["message"] for issue in result["evaluations"][0]["issues"])


@pytest.mark.asyncio
async def test_office_workflow_ppt_quality_stats_pass_with_complete_metrics() -> None:
    from agent.domains.office.workflow import evaluate_node

    state = {
        "format": "pptx",
        "operation": "create",
        "write_required": True,
        "intermediate_results": [
            {
                "output": """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"deck.pptx","format":"pptx","role":"primary"}],"stats":{"slide_count":6,"content_slide_count":4,"notes_slide_count":4,"transition_slide_count":5,"visual_slide_count":4,"text_only_slide_count":0,"layout_variety_count":3,"picture_count":1,"chart_count":1,"table_count":0,"qa_checks":["view_stats","view_annotated","validate"]}}
```"""
            }
        ],
    }

    result = await evaluate_node(state)

    assert result["evaluations"][0]["passed"] is True
    assert result["final_result"]
