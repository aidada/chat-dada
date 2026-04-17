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


def test_infer_default_create_file_rewrites_generic_llm_filename() -> None:
    from agent.domains.office.workflow import _infer_default_create_file

    filename = _infer_default_create_file(
        "在下载文件夹下，为我生成一个 PPT，主题为 论如何在新时代 AI 环境下，对孩子进行现代化教育，一共 10 页",
        "ai.pptx",
        "pptx",
    )

    assert filename == "ai-education-children.pptx"


@pytest.mark.asyncio
async def test_office_preflight_increases_inner_limit_for_large_visual_deck() -> None:
    from agent.domains.office.workflow import OFFICE_INNER_RECURSION_LIMIT, preflight_node

    result = await preflight_node(
        {
            "goal": "在下载文件夹下，为我生成一个 PPT，主题为 AI 时代儿童现代化教育，一共 10 页，图文并茂并带动画",
            "format_hint": "pptx",
            "operation_hint": "create",
            "file_hint": "",
            "source_files": [],
        }
    )

    assert result["build_batch_size"] == 3
    assert result["inner_recursion_limit"] > OFFICE_INNER_RECURSION_LIMIT
    assert result["quality_profile"]["animations"] is True
    assert result["quality_profile"]["visuals"] is True


@pytest.mark.asyncio
async def test_office_planning_node_creates_deck_plan_and_batches() -> None:
    from agent.domains.office.workflow import planning_node

    result = await planning_node(
        {
            "goal": "在下载文件夹下，为我生成一个 10 页 PPT，主题为 AI 时代儿童现代化教育",
            "requested_slide_count": 10,
            "build_batch_size": 3,
            "default_create_file": "ai-era-child-modern-education.pptx",
            "cost_ledger": {},
        }
    )

    plan = result["deck_plan"]
    assert plan["slide_count"] == 10
    assert len(plan["batches"]) == 4
    assert plan["batches"][0]["slide_start"] == 1
    assert plan["batches"][0]["slide_end"] == 3
    assert result["current_stage"] == "build"
    assert "takeaway" in plan["slides"][0]
    assert "layout_type" in plan["slides"][0]
    assert "visual_requirements" in plan["slides"][0]
    assert "transition_required" in plan["slides"][0]
    assert "notes_required" in plan["slides"][0]
    assert "objective" in plan["batches"][0]


@pytest.mark.asyncio
async def test_office_planning_node_refines_generic_filename_from_plan_title() -> None:
    from agent.domains.office.workflow import planning_node

    result = await planning_node(
        {
            "goal": "在下载文件夹下，为我生成一个 10 页 PPT，主题为 AI 时代儿童现代化教育",
            "requested_slide_count": 10,
            "build_batch_size": 3,
            "default_create_file": "ai.pptx",
            "format": "pptx",
            "operation": "create",
            "cost_ledger": {},
        }
    )

    assert result["default_create_file"].endswith(".pptx")
    assert result["default_create_file"] != "ai.pptx"


def test_office_route_after_build_loops_until_all_batches_written() -> None:
    from agent.domains.office.workflow import route_after_build

    assert route_after_build({"current_stage": "build"}) == "build"
    assert route_after_build({"current_stage": "qa_fix"}) == "qa_fix"
    assert route_after_build({"terminal_status": "error"}) == "finalize"


def test_office_route_after_qa_fix_retries_then_finalizes() -> None:
    from agent.domains.office.workflow import route_after_qa_fix

    assert route_after_qa_fix({"current_stage": "build"}) == "build"
    assert route_after_qa_fix({"current_stage": "finalize"}) == "finalize"


def test_strategy_selector_returns_docx_and_xlsx_specific_strategies() -> None:
    from agent.domains.office.strategies import get_strategy_for_format
    from agent.domains.office.strategies.docx import DocxStrategy
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    assert isinstance(get_strategy_for_format("docx"), DocxStrategy)
    assert isinstance(get_strategy_for_format("xlsx"), XlsxStrategy)


def test_ppt_strategy_build_input_sections_include_batch_context() -> None:
    from agent.domains.office.strategies.ppt import PptStrategy

    strategy = PptStrategy()
    plan = strategy.build_plan(
        goal="在下载文件夹下，为我生成一个 10 页 PPT，主题为 AI 时代儿童现代化教育",
        requested_slide_count=10,
        build_batch_size=3,
        default_create_file="ai-era-child-modern-education.pptx",
    )
    sections = strategy.build_input_sections(
        goal="在下载文件夹下，为我生成一个 10 页 PPT，主题为 AI 时代儿童现代化教育",
        operation="create",
        format_hint="pptx",
        runtime_target="desktop",
        default_create_file="ai-era-child-modern-education.pptx",
        requested_slide_count=10,
        build_batch_size=3,
        source_files=[],
        context="",
        qa_feedback="",
        plan=plan,
        current_batch_index=1,
        repair_mode=False,
    )

    rendered = "\n".join(sections)
    assert "build_batch_size: 3" in rendered
    assert "current_batch_index: 1" in rendered
    assert "current_batch_slide_range: 4-6" in rendered


def test_ppt_strategy_validate_plan_repairs_missing_contract_fields() -> None:
    from agent.domains.office.strategies.ppt import PptStrategy

    strategy = PptStrategy()
    plan, issues = strategy.validate_plan(
        plan={
            "title": "",
            "slide_count": 4,
            "slides": [{"title": "封面"}],
            "batches": [],
        },
        goal="在下载文件夹下，为我生成一个 4 页 PPT，主题为 AI 时代儿童现代化教育",
        requested_slide_count=4,
        build_batch_size=2,
        default_create_file="ai-era-child-modern-education.pptx",
    )

    assert issues
    assert plan["title"]
    assert plan["slide_count"] == 4
    assert len(plan["slides"]) == 4
    assert len(plan["batches"]) == 2
    assert "takeaway" in plan["slides"][1]
    assert "layout_type" in plan["slides"][1]
    assert "visual_requirements" in plan["slides"][1]
    assert "transition_required" in plan["slides"][1]
    assert "notes_required" in plan["slides"][1]


def test_ppt_strategy_quality_metrics_pass_for_long_deck() -> None:
    from agent.domains.office.strategies.ppt import PptStrategy

    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats={
            "slide_count": 10,
            "content_slide_count": 8,
            "notes_slide_count": 8,
            "transition_slide_count": 9,
            "visual_slide_count": 8,
            "text_only_slide_count": 0,
            "layout_variety_count": 4,
            "picture_count": 3,
            "chart_count": 1,
            "table_count": 1,
            "qa_checks": ["view_stats", "view_annotated", "validate"],
        },
    )

    assert issues == []


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
    assert result.budget["cost_ledger"]["domain"] == "office"


@pytest.mark.asyncio
async def test_office_domain_terminal_partial_progress_included_in_review() -> None:
    from agent.domains.office.orchestrated import run_office_domain_orchestrated

    with (
        patch(
            "agent.domains.office.orchestrated.stream_nested_graph",
            new=AsyncMock(
                return_value={
                    "final_result": "Office 任务已中止：内层 agent 超过 84 步仍未收敛",
                    "terminal_status": "bounded_failure",
                    "terminal_reason": "inner_recursion_limit",
                    "partial_progress": {
                        "stage": "build",
                        "completed_pages": 6,
                        "requested_pages": 10,
                        "current_batch_index": 2,
                        "current_batch_slide_range": [7, 9],
                        "reason": "inner_recursion_limit",
                    },
                    "step_history": [{"strategy": "sequential"}],
                    "cost_ledger": {"task_id": "office_partial", "domain": "office"},
                }
            ),
        ),
        patch("agent.domains.office.orchestrated.infer_office_runtime_target", return_value="desktop"),
    ):
        result = await run_office_domain_orchestrated({"query": "做一个 10 页 PPT", "task_id": "office_partial"})

    assert result.status == "error"
    assert result.review["partial_progress"]["completed_pages"] == 6
    assert "已完成页数: 6" in result.result


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


@pytest.mark.asyncio
async def test_office_qa_fix_requests_repair_when_ppt_stats_fail() -> None:
    from agent.domains.office.workflow import qa_fix_node

    state = {
        "format": "pptx",
        "operation": "create",
        "write_required": True,
        "qa_fix_round": 0,
        "max_qa_fix_rounds": 2,
        "intermediate_results": [
            {
                "output": """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"deck.pptx","format":"pptx","role":"primary"}],"stats":{"slide_count":6,"content_slide_count":4,"notes_slide_count":0,"transition_slide_count":0,"visual_slide_count":0,"text_only_slide_count":2,"layout_variety_count":1,"picture_count":0,"chart_count":0,"table_count":0,"qa_checks":["validate"]}}
```"""
            }
        ],
    }

    result = await qa_fix_node(state)

    assert result["current_stage"] == "build"
    assert result["repair_mode"] is True
    assert result["qa_fix_round"] == 1
    assert result["quality_report"]["status"] == "fixable"
    assert result["quality_report"]["issue_count"] > 0
    assert result["quality_report"]["stats_summary"]["slide_count"] == 6


@pytest.mark.asyncio
async def test_office_qa_fix_hard_fails_after_round_exhaustion() -> None:
    from agent.domains.office.workflow import qa_fix_node

    state = {
        "format": "pptx",
        "operation": "create",
        "write_required": True,
        "requested_slide_count": 10,
        "qa_fix_round": 1,
        "max_qa_fix_rounds": 1,
        "intermediate_results": [
            {
                "output": """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"deck.pptx","format":"pptx","role":"primary"}],"stats":{"slide_count":10,"content_slide_count":8,"notes_slide_count":0,"transition_slide_count":0,"visual_slide_count":0,"text_only_slide_count":3,"layout_variety_count":1,"picture_count":0,"chart_count":0,"table_count":0,"qa_checks":["validate"]}}
```"""
            }
        ],
    }

    result = await qa_fix_node(state)

    assert result["current_stage"] == "finalize"
    assert result["terminal_status"] == "quality_gate_failed"
    assert result["quality_report"]["status"] == "hard_fail"
    assert result["partial_progress"]["reason"] == "qa_fix_round_exhausted"


@pytest.mark.asyncio
async def test_run_build_stage_recursion_failure_returns_partial_progress() -> None:
    from langgraph.errors import GraphRecursionError

    from agent.domains.office.core.build import run_build_stage
    from agent.domains.office.strategies.ppt import PptStrategy

    strategy = PptStrategy()
    plan = strategy.build_plan(
        goal="在下载文件夹下，为我生成一个 10 页 PPT，主题为 AI 时代儿童现代化教育",
        requested_slide_count=10,
        build_batch_size=3,
        default_create_file="ai-era-child-modern-education.pptx",
    )

    state = {
        "goal": "在下载文件夹下，为我生成一个 10 页 PPT，主题为 AI 时代儿童现代化教育",
        "task_id": "office_build_recursion",
        "format": "pptx",
        "operation": "create",
        "runtime_target_hint": "desktop",
        "default_create_file": "ai-era-child-modern-education.pptx",
        "requested_slide_count": 10,
        "build_batch_size": 3,
        "deck_plan": plan,
        "current_batch_index": 1,
        "completed_pages": 3,
        "inner_recursion_limit": 84,
        "allowed_source_files": [],
        "repair_mode": False,
        "intermediate_results": [],
        "evaluations": [],
        "cost_ledger": {"task_id": "office_build_recursion", "domain": "office"},
    }

    with (
        patch("agent.domains.office.core.build.get_config", return_value={"configurable": {}}),
        patch("agent.domains.office.core.build.resolve_deepagents_runtime", return_value=([], object())),
        patch("agent.domains.office.core.build.build_chat_model", return_value=object()),
        patch("agent.domains.office.core.build.build_officecli_skill_bundle", return_value=""),
        patch("agent.domains.office.core.build.create_deep_agent", return_value=object()),
        patch(
            "agent.domains.office.core.build.stream_nested_graph",
            new=AsyncMock(side_effect=GraphRecursionError("recursion")),
        ),
    ):
        result = await run_build_stage(
            state,
            strategy=strategy,
            system_template="{format_hint}{operation}{runtime_target}{default_create_file}{source_files_block}{format_specific_guidance}{phase_guidance}{skill_content}",
            format_specific_guidance="",
            office_model_role="orchestrator",
            subagents=[],
        )

    assert result["terminal_reason"] == "inner_recursion_limit"
    assert result["partial_progress"]["completed_pages"] == 3
    assert result["partial_progress"]["current_batch_slide_range"] == [4, 6]
