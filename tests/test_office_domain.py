from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, patch
from typing import Any

import pytest
from langchain_core.messages import AIMessage


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


@pytest.mark.asyncio
async def test_docx_planning_node_uses_llm_structured_goal_constraints() -> None:
    import agent.domains.office.workflow as workflow_module

    class FakeLLM:
        async def ainvoke(self, _messages):
            return AIMessage(
                content='{"section_headings":["执行摘要","实施计划"],"formatting_instructions":["preserve numbering"]}'
            )

    with patch("core.models.get_llm", return_value=FakeLLM()):
        workflow_module = importlib.reload(workflow_module)
        try:
            result = await workflow_module.planning_node(
                {
                    "goal": "参考方案模板，更新项目方案中的执行摘要和实施计划，并保留附录与致谢。",
                    "format": "docx",
                    "operation": "edit",
                    "requested_slide_count": 0,
                    "build_batch_size": 1,
                    "default_create_file": "project-plan.docx",
                    "goal_constraints": {
                        "hard_requirements": ["执行摘要", "实施计划", "preserve numbering"],
                    },
                    "reference_structure_constraints": {
                        "units": [{"name": "执行摘要"}, {"name": "实施计划"}, {"name": "附录"}]
                    },
                    "reference_style_constraints": {"style_tokens": {"heading_style": "Heading1"}},
                    "existing_document_profile": {
                        "units": ["执行摘要", "实施计划", "附录", "致谢"],
                        "protected_units": ["附录", "致谢"],
                    },
                    "cost_ledger": {},
                }
            )
        finally:
            importlib.reload(workflow_module)

    merged = result["task_profile"]["merged_constraints"]
    assert merged["goal_constraints"]["section_headings"] == ["执行摘要", "实施计划"]
    assert merged["goal_constraints"]["formatting_instructions"] == ["preserve numbering"]
    assert [section["heading"] for section in result["deck_plan"]["sections"]] == ["执行摘要", "实施计划"]
    assert result["deck_plan"]["sections"][0]["style_requirements"]["formatting_instructions"] == ["preserve numbering"]


@pytest.mark.asyncio
async def test_planning_node_carries_reference_constraints_into_task_profile() -> None:
    from agent.domains.office.strategies.ppt import PptStrategy
    from agent.domains.office.workflow import planning_node

    class IncompletePlanPptStrategy(PptStrategy):
        def __init__(self) -> None:
            self._build_plan_calls = 0

        def build_plan(self, **kwargs):
            self._build_plan_calls += 1
            plan = super().build_plan(**kwargs)
            if self._build_plan_calls == 1:
                return {
                    **plan,
                    "slides": [{"index": 1}, {"index": 2}],
                    "batches": [],
                }
            return plan

    with patch(
        "agent.domains.office.workflow.get_strategy_for_format",
        return_value=IncompletePlanPptStrategy(),
    ):
        result = await planning_node(
            {
                "goal": "按参考案例生成 8 页产品介绍 PPT",
                "format": "pptx",
                "operation": "create",
                "requested_slide_count": 8,
                "build_batch_size": 3,
                "default_create_file": "product-intro.pptx",
                "task_profile": {"reference_files": ["example.pptx"]},
                "goal_constraints": {"goal": "按参考案例生成 8 页产品介绍 PPT"},
                "reference_structure_constraints": {"units": [{"name": "封面"}, {"name": "问题"}, {"name": "方案"}]},
                "reference_style_constraints": {"style_tokens": {"theme": "blue"}},
                "cost_ledger": {},
            }
        )

    assert result["task_profile"]["target_filename"].endswith(".pptx")
    assert result["task_profile"]["merged_constraints"]["goal_constraints"]["goal"] == "按参考案例生成 8 页产品介绍 PPT"
    assert result["task_profile"]["merged_constraints"]["reference_structure_constraints"]["units"] == [
        {"name": "封面"},
        {"name": "问题"},
        {"name": "方案"},
    ]
    assert result["task_profile"]["merged_constraints"]["reference_style_constraints"]["style_tokens"] == {"theme": "blue"}
    assert result["planning_summary"]["slide_count"] == 8
    assert result["deck_plan"]["slides"][0]["title"] == "封面"
    assert result["deck_plan"]["slides"][1]["title"] == "问题"


@pytest.mark.asyncio
async def test_planning_node_refines_filename_when_format_is_inferred() -> None:
    from agent.domains.office.workflow import planning_node

    result = await planning_node(
        {
            "goal": "在下载文件夹下，为我生成一个 10 页 PPT，主题为 AI 时代儿童现代化教育",
            "requested_slide_count": 10,
            "build_batch_size": 3,
            "default_create_file": "ai.pptx",
            "operation": "create",
            "cost_ledger": {},
        }
    )

    assert result["task_profile"]["target_filename"].endswith(".pptx")
    assert result["task_profile"]["target_filename"] != "ai.pptx"


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


def test_xlsx_strategy_builds_sheet_plan_from_goal_and_reference() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成一个预算分析表，包含 RawData、Summary、Dashboard",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget-analysis.xlsx",
        merged_constraints={
            "goal_constraints": {"hard_requirements": ["RawData", "Summary", "Dashboard"]},
            "reference_structure_constraints": {"units": [{"name": "RawData"}, {"name": "Summary"}]},
            "reference_style_constraints": {"style_tokens": {"summary_position": "top"}},
        },
    )

    assert plan["sheet_count"] == 3
    assert plan["sheets"][0]["name"] == "RawData"
    assert plan["sheets"][2]["sheet_type"] == "dashboard"


def test_docx_strategy_builds_section_plan_from_goal_and_reference() -> None:
    from agent.domains.office.strategies.docx import DocxStrategy

    plan = DocxStrategy().build_plan(
        goal="生成一份项目方案，包含背景、目标、实施计划、风险控制",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="project-plan.docx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": ["背景", "目标", "实施计划", "风险控制", "preserve formatting"],
                "section_headings": ["背景", "目标", "实施计划", "风险控制"],
                "formatting_instructions": ["preserve formatting"],
            },
            "reference_structure_constraints": {
                "units": [{"name": "背景"}, {"name": "目标"}, {"name": "附录"}, {"name": "致谢"}]
            },
            "reference_style_constraints": {"style_tokens": {"heading_style": "Heading1"}},
        },
    )

    assert plan["section_count"] == 4
    assert [section["heading"] for section in plan["sections"]] == ["背景", "目标", "实施计划", "风险控制"]
    assert plan["sections"][0]["heading"] == "背景"
    assert plan["sections"][2]["content_mode"] == "mixed"
    assert plan["sections"][0]["style_requirements"]["formatting_instructions"] == ["preserve formatting"]


def test_docx_strategy_ignores_instruction_like_hard_requirements() -> None:
    from agent.domains.office.strategies.docx import DocxStrategy

    plan = DocxStrategy().build_plan(
        goal="生成一份项目方案",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="project-plan.docx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": ["preserve formatting", "Executive Summary", "keep numbering consistent", "Project Scope"],
                "section_headings": ["Executive Summary", "Project Scope"],
                "formatting_instructions": ["preserve formatting", "keep numbering consistent"],
            }
        },
    )

    assert [section["heading"] for section in plan["sections"]] == ["Executive Summary", "Project Scope"]
    assert plan["sections"][0]["style_requirements"]["formatting_instructions"] == [
        "preserve formatting",
        "keep numbering consistent",
    ]


def test_xlsx_strategy_ignores_non_sheet_like_hard_requirements() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成一个预算分析表，保留公式并重命名汇总表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget-analysis.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": ["preserve formulas", "RawData", "rename summary sheet", "Dashboard"]
            },
            "reference_structure_constraints": {"units": [{"name": "Summary"}]},
        },
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == ["RawData", "Dashboard", "Summary"]


def test_xlsx_strategy_accepts_realistic_multi_word_sheet_name() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成区域销售预算表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="regional-sales.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": ["Regional Sales Summary", "preserve formulas", "Monthly Budget Forecast"]
            },
        },
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == [
        "Regional Sales Summary",
        "Monthly Budget Forecast",
    ]


def test_xlsx_strategy_ignores_string_hard_requirements_value() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": "RawData",
            },
            "reference_structure_constraints": {"units": [{"name": "Summary"}]},
        },
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == ["Summary"]


def test_xlsx_strategy_accepts_numeric_and_light_punctuation_sheet_names() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成财务预算表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": [
                    "Q1 Summary",
                    "2026 Budget",
                    "Sales FY25",
                    "Summary (Final)",
                    "Q1 v1.0",
                    "Ops;2026",
                    "Sales!",
                ]
            },
        },
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == [
        "Q1 Summary",
        "2026 Budget",
        "Sales FY25",
        "Summary (Final)",
        "Q1 v1.0",
        "Ops;2026",
        "Sales!",
    ]


def test_xlsx_strategy_accepts_comma_separated_explicit_sheet_name() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成财务预算表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": ["Summary, Final", "preserve formulas", "rename summary sheet"]
            },
        },
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == ["Summary, Final"]


def test_xlsx_strategy_rejects_overlong_sheet_name() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    long_name = "Budget Forecast For International Sales 2026"
    plan = XlsxStrategy().build_plan(
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": [long_name],
            },
            "reference_structure_constraints": {"units": [{"name": "Summary"}]},
        },
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == ["Summary"]


def test_xlsx_strategy_rejects_forbidden_character_sheet_name() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": ["Budget/2026"],
            },
            "reference_structure_constraints": {"units": [{"name": "Summary"}]},
        },
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == ["Summary"]


def test_xlsx_strategy_build_plan_dedupes_case_insensitive_sheet_names() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": ["Summary", "summary", "Dashboard"],
            },
        },
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == ["Summary", "Dashboard"]


def test_xlsx_strategy_validate_plan_preserves_existing_sheets_and_batches() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    existing_sheet = {
        "name": "Budget",
        "purpose": "Track approved budget lines.",
        "sheet_type": "summary",
        "columns": [{"name": "Department", "type": "text"}],
        "table_regions": [{"name": "BudgetTable", "range_hint": "A1:B10"}],
        "formula_regions": [{"name": "BudgetFormula", "range_hint": "D2:D10"}],
        "chart_regions": [],
        "validation_rules": [{"kind": "required_headers", "target": "A1:B1"}],
    }
    existing_batch = {
        "index": 0,
        "sheet_start": 1,
        "sheet_end": 1,
        "sheet_names": ["Budget"],
        "slide_start": 1,
        "slide_end": 1,
        "slide_titles": ["Budget"],
        "slide_roles": ["summary"],
    }

    plan, issues = strategy.validate_plan(
        plan={
            "title": "Budget Workbook",
            "sheet_count": 1,
            "sheets": [existing_sheet],
            "batches": [existing_batch],
        },
        goal="生成预算表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
    )

    assert issues == []
    assert plan["sheets"] == [existing_sheet]
    assert plan["batches"] == [existing_batch]


def test_xlsx_strategy_validate_plan_rebuilds_case_insensitive_duplicate_sheet_names() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    plan, issues = strategy.validate_plan(
        plan={
            "title": "Workbook",
            "sheet_count": 2,
            "sheets": [
                {
                    "name": "Summary",
                    "purpose": "Top-level metrics.",
                    "sheet_type": "summary",
                    "columns": [],
                    "table_regions": [],
                    "formula_regions": [],
                    "chart_regions": [],
                    "validation_rules": [],
                },
                {
                    "name": "summary",
                    "purpose": "Duplicate metrics.",
                    "sheet_type": "summary",
                    "columns": [],
                    "table_regions": [],
                    "formula_regions": [],
                    "chart_regions": [],
                    "validation_rules": [],
                },
            ],
            "batches": [],
        },
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "reference_structure_constraints": {"units": [{"name": "Summary"}, {"name": "Dashboard"}]},
        },
    )

    assert "duplicate_sheet_name" in issues
    assert [sheet["name"] for sheet in plan["sheets"]] == ["Summary", "Dashboard"]


def test_xlsx_strategy_validate_plan_rebuilds_invalid_preserved_sheet_name() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    plan, issues = strategy.validate_plan(
        plan={
            "title": "Workbook",
            "sheet_count": 1,
            "sheets": [
                {
                    "name": "Budget/2026",
                    "purpose": "Track budget.",
                    "sheet_type": "summary",
                    "columns": [],
                    "table_regions": [],
                    "formula_regions": [],
                    "chart_regions": [],
                    "validation_rules": [],
                }
            ],
            "batches": [],
        },
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "reference_structure_constraints": {"units": [{"name": "Summary"}]},
        },
    )

    assert "invalid_sheet_name" in issues
    assert [sheet["name"] for sheet in plan["sheets"]] == ["Summary"]


def test_xlsx_strategy_validate_plan_falls_back_on_malformed_numeric_fields() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    plan, issues = strategy.validate_plan(
        plan={
            "title": "Workbook",
            "sheet_count": "three",
            "sheets": [
                {
                    "name": "Summary",
                    "purpose": "Top-level metrics.",
                    "sheet_type": "summary",
                    "columns": [],
                    "table_regions": [],
                    "formula_regions": [],
                    "chart_regions": [],
                    "validation_rules": [],
                }
            ],
            "batches": [
                {
                    "index": 0,
                    "sheet_start": "first",
                    "sheet_end": "last",
                    "sheet_names": ["Summary"],
                }
            ],
        },
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "reference_structure_constraints": {"units": [{"name": "Summary"}, {"name": "Dashboard"}]},
        },
    )

    assert issues == []
    assert plan["sheet_count"] == 1
    assert plan["batches"] == [
        {
            "index": 0,
            "sheet_start": 1,
            "sheet_end": 1,
            "sheet_names": ["Summary"],
            "slide_start": 1,
            "slide_end": 1,
            "slide_titles": ["Summary"],
            "slide_roles": ["summary"],
        }
    ]


def test_xlsx_strategy_validate_plan_rebuilds_duplicate_preserved_batches() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    existing_sheets = [
        {
            "name": "Summary",
            "purpose": "Top-level metrics.",
            "sheet_type": "summary",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        },
        {
            "name": "Dashboard",
            "purpose": "Present KPI charts.",
            "sheet_type": "dashboard",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        },
    ]

    plan, issues = strategy.validate_plan(
        plan={
            "title": "Workbook",
            "sheet_count": 2,
            "sheets": existing_sheets,
            "batches": [
                {
                    "index": 0,
                    "sheet_start": 1,
                    "sheet_end": 1,
                    "sheet_names": ["Summary"],
                },
                {
                    "index": 1,
                    "sheet_start": 1,
                    "sheet_end": 1,
                    "sheet_names": ["Summary"],
                },
            ],
        },
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
    )

    assert issues == []
    assert plan["batches"] == [
        {
            "index": 0,
            "sheet_start": 1,
            "sheet_end": 1,
            "sheet_names": ["Summary"],
            "slide_start": 1,
            "slide_end": 1,
            "slide_titles": ["Summary"],
            "slide_roles": ["summary"],
        },
        {
            "index": 1,
            "sheet_start": 2,
            "sheet_end": 2,
            "sheet_names": ["Dashboard"],
            "slide_start": 2,
            "slide_end": 2,
            "slide_titles": ["Dashboard"],
            "slide_roles": ["dashboard"],
        },
    ]


def test_xlsx_strategy_validate_plan_rebuilds_gap_or_overlap_in_preserved_batches() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    existing_sheets = [
        {
            "name": "RawData",
            "purpose": "Store source records.",
            "sheet_type": "raw_data",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        },
        {
            "name": "Summary",
            "purpose": "Top-level metrics.",
            "sheet_type": "summary",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        },
        {
            "name": "Dashboard",
            "purpose": "Present KPI charts.",
            "sheet_type": "dashboard",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        },
    ]

    plan, issues = strategy.validate_plan(
        plan={
            "title": "Workbook",
            "sheet_count": 3,
            "sheets": existing_sheets,
            "batches": [
                {
                    "index": 0,
                    "sheet_start": 1,
                    "sheet_end": 1,
                    "sheet_names": ["RawData"],
                },
                {
                    "index": 1,
                    "sheet_start": 3,
                    "sheet_end": 3,
                    "sheet_names": ["Dashboard"],
                },
            ],
        },
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
    )

    assert issues == []
    assert plan["batches"] == [
        {
            "index": 0,
            "sheet_start": 1,
            "sheet_end": 1,
            "sheet_names": ["RawData"],
            "slide_start": 1,
            "slide_end": 1,
            "slide_titles": ["RawData"],
            "slide_roles": ["raw_data"],
        },
        {
            "index": 1,
            "sheet_start": 2,
            "sheet_end": 2,
            "sheet_names": ["Summary"],
            "slide_start": 2,
            "slide_end": 2,
            "slide_titles": ["Summary"],
            "slide_roles": ["summary"],
        },
        {
            "index": 2,
            "sheet_start": 3,
            "sheet_end": 3,
            "sheet_names": ["Dashboard"],
            "slide_start": 3,
            "slide_end": 3,
            "slide_titles": ["Dashboard"],
            "slide_roles": ["dashboard"],
        },
    ]


def test_xlsx_strategy_validate_plan_preserves_valid_multi_sheet_batch_set() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    existing_sheets = [
        {
            "name": "RawData",
            "purpose": "Store source records.",
            "sheet_type": "raw_data",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        },
        {
            "name": "Summary",
            "purpose": "Top-level metrics.",
            "sheet_type": "summary",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        },
        {
            "name": "Dashboard",
            "purpose": "Present KPI charts.",
            "sheet_type": "dashboard",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        },
    ]

    plan, issues = strategy.validate_plan(
        plan={
            "title": "Workbook",
            "sheet_count": 3,
            "sheets": existing_sheets,
            "batches": [
                {
                    "index": 0,
                    "sheet_start": 1,
                    "sheet_end": 2,
                    "sheet_names": ["RawData", "Summary"],
                },
                {
                    "index": 1,
                    "sheet_start": 3,
                    "sheet_end": 3,
                    "sheet_names": ["Dashboard"],
                },
            ],
        },
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
    )

    assert issues == []
    assert plan["batches"] == [
        {
            "index": 0,
            "sheet_start": 1,
            "sheet_end": 2,
            "sheet_names": ["RawData", "Summary"],
            "slide_start": 1,
            "slide_end": 2,
            "slide_titles": ["RawData", "Summary"],
            "slide_roles": ["raw_data", "summary"],
        },
        {
            "index": 1,
            "sheet_start": 3,
            "sheet_end": 3,
            "sheet_names": ["Dashboard"],
            "slide_start": 3,
            "slide_end": 3,
            "slide_titles": ["Dashboard"],
            "slide_roles": ["dashboard"],
        },
    ]


def test_xlsx_strategy_sanitizes_table_region_identifier_from_multi_word_sheet_name() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成区域销售预算表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="regional-sales.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": ["Regional Sales Summary"],
            },
        },
    )

    assert plan["sheets"][0]["table_regions"] == [{"name": "RegionalSalesSummaryTable", "range_hint": "A1:C20"}]


def test_xlsx_strategy_sanitizes_numeric_leading_table_region_identifier() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": ["2026 Budget"],
            },
        },
    )

    assert plan["sheets"][0]["name"] == "2026 Budget"
    assert plan["sheets"][0]["table_regions"] == [{"name": "tbl_2026BudgetTable", "range_hint": "A1:C20"}]


def test_xlsx_strategy_generates_unique_table_region_identifiers_for_colliding_sheet_names() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "goal_constraints": {
                "hard_requirements": ["A B", "AB"],
            },
        },
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == ["A B", "AB"]
    assert plan["sheets"][0]["table_regions"] == [{"name": "ABTable", "range_hint": "A1:C20"}]
    assert plan["sheets"][1]["table_regions"] == [{"name": "ABTable_2", "range_hint": "A1:C20"}]


def test_xlsx_strategy_ignores_descriptive_reference_unit_names() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="生成预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={
            "reference_structure_constraints": {
                "units": [
                    {"name": "preserve formulas and existing formatting"},
                    {"name": "Summary"},
                    {"name": "rename summary sheet to final"},
                    {"name": "Dashboard"},
                ]
            },
        },
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == ["Summary", "Dashboard"]


def test_xlsx_strategy_validate_plan_avoids_collision_with_preserved_table_region_identifier() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    plan, issues = strategy.validate_plan(
        plan={
            "title": "Workbook",
            "sheet_count": 2,
            "sheets": [
                {
                    "name": "Sales",
                    "purpose": "Existing sales data.",
                    "sheet_type": "worksheet",
                    "columns": [],
                    "table_regions": [{"name": "SalesTable", "range_hint": "A1:C20"}],
                    "formula_regions": [],
                    "chart_regions": [],
                    "validation_rules": [],
                },
                {
                    "name": "Sales!",
                    "purpose": "New sales data.",
                    "sheet_type": "worksheet",
                    "columns": [],
                    "formula_regions": [],
                    "chart_regions": [],
                    "validation_rules": [],
                },
            ],
            "batches": [
                {
                    "index": 0,
                    "sheet_start": 1,
                    "sheet_end": 2,
                    "sheet_names": ["Sales", "Sales!"],
                }
            ],
        },
        goal="生成销售分析表",
        requested_slide_count=0,
        build_batch_size=2,
        default_create_file="sales.xlsx",
    )

    assert issues == []
    assert plan["sheets"][0]["table_regions"] == [{"name": "SalesTable", "range_hint": "A1:C20"}]
    assert plan["sheets"][1]["table_regions"] == [{"name": "SalesTable_2", "range_hint": "A1:C20"}]


def test_xlsx_strategy_validate_plan_avoids_collision_when_preserved_identifier_comes_later() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    plan, issues = strategy.validate_plan(
        plan={
            "title": "Workbook",
            "sheet_count": 2,
            "sheets": [
                {
                    "name": "Sales",
                    "purpose": "Generated sales data.",
                    "sheet_type": "worksheet",
                    "columns": [],
                    "formula_regions": [],
                    "chart_regions": [],
                    "validation_rules": [],
                },
                {
                    "name": "Sales!",
                    "purpose": "Preserved sales data.",
                    "sheet_type": "worksheet",
                    "columns": [],
                    "table_regions": [{"name": "SalesTable", "range_hint": "A1:C20"}],
                    "formula_regions": [],
                    "chart_regions": [],
                    "validation_rules": [],
                },
            ],
            "batches": [
                {
                    "index": 0,
                    "sheet_start": 1,
                    "sheet_end": 2,
                    "sheet_names": ["Sales", "Sales!"],
                }
            ],
        },
        goal="生成销售分析表",
        requested_slide_count=0,
        build_batch_size=2,
        default_create_file="sales.xlsx",
    )

    assert issues == []
    assert plan["sheets"][0]["table_regions"] == [{"name": "SalesTable_2", "range_hint": "A1:C20"}]
    assert plan["sheets"][1]["table_regions"] == [{"name": "SalesTable", "range_hint": "A1:C20"}]


def test_xlsx_strategy_validate_plan_normalizes_stale_alias_fields_in_preserved_batch() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    existing_sheets = [
        {
            "name": "Budget",
            "purpose": "Track approved budget lines.",
            "sheet_type": "summary",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        }
    ]

    plan, issues = strategy.validate_plan(
        plan={
            "title": "Budget Workbook",
            "sheet_count": 1,
            "sheets": existing_sheets,
            "batches": [
                {
                    "index": 0,
                    "sheet_start": 1,
                    "sheet_end": 1,
                    "sheet_names": ["Budget"],
                    "slide_start": 1,
                    "slide_end": 1,
                    "slide_titles": ["Old Title"],
                    "slide_roles": ["worksheet"],
                }
            ],
        },
        goal="生成预算表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
    )

    assert issues == []
    assert plan["batches"] == [
        {
            "index": 0,
            "sheet_start": 1,
            "sheet_end": 1,
            "sheet_names": ["Budget"],
            "slide_start": 1,
            "slide_end": 1,
            "slide_titles": ["Budget"],
            "slide_roles": ["summary"],
        }
    ]


def test_xlsx_strategy_validate_plan_normalizes_stale_alias_ranges_in_preserved_batch() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    existing_sheets = [
        {
            "name": "Budget",
            "purpose": "Track approved budget lines.",
            "sheet_type": "summary",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        }
    ]

    plan, issues = strategy.validate_plan(
        plan={
            "title": "Budget Workbook",
            "sheet_count": 1,
            "sheets": existing_sheets,
            "batches": [
                {
                    "index": 0,
                    "sheet_start": 1,
                    "sheet_end": 1,
                    "sheet_names": ["Budget"],
                    "slide_start": 7,
                    "slide_end": 9,
                    "slide_titles": ["Budget"],
                    "slide_roles": ["summary"],
                }
            ],
        },
        goal="生成预算表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
    )

    assert issues == []
    assert plan["batches"] == [
        {
            "index": 0,
            "sheet_start": 1,
            "sheet_end": 1,
            "sheet_names": ["Budget"],
            "slide_start": 1,
            "slide_end": 1,
            "slide_titles": ["Budget"],
            "slide_roles": ["summary"],
        }
    ]


def test_xlsx_strategy_validate_plan_rebuilds_inconsistent_preserved_batch() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    existing_sheets = [
        {
            "name": "RawData",
            "purpose": "Store source records.",
            "sheet_type": "raw_data",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        },
        {
            "name": "Summary",
            "purpose": "Aggregate metrics.",
            "sheet_type": "summary",
            "columns": [],
            "table_regions": [],
            "formula_regions": [],
            "chart_regions": [],
            "validation_rules": [],
        },
    ]

    plan, issues = strategy.validate_plan(
        plan={
            "title": "Workbook",
            "sheet_count": 2,
            "sheets": existing_sheets,
            "batches": [
                {
                    "index": 0,
                    "sheet_start": 1,
                    "sheet_end": 2,
                    "sheet_names": ["Summary", "RawData"],
                    "slide_start": 1,
                    "slide_end": 2,
                    "slide_titles": ["Summary", "RawData"],
                    "slide_roles": ["summary", "raw_data"],
                }
            ],
        },
        goal="生成预算表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
    )

    assert issues == []
    assert plan["batches"] == [
        {
            "index": 0,
            "sheet_start": 1,
            "sheet_end": 1,
            "sheet_names": ["RawData"],
            "slide_start": 1,
            "slide_end": 1,
            "slide_titles": ["RawData"],
            "slide_roles": ["raw_data"],
        },
        {
            "index": 1,
            "sheet_start": 2,
            "sheet_end": 2,
            "sheet_names": ["Summary"],
            "slide_start": 2,
            "slide_end": 2,
            "slide_titles": ["Summary"],
            "slide_roles": ["summary"],
        },
    ]


def test_xlsx_strategy_validate_plan_does_not_expand_string_list_fields() -> None:
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    strategy = XlsxStrategy()
    plan, issues = strategy.validate_plan(
        plan={
            "title": "Workbook",
            "sheet_count": 1,
            "sheets": [
                {
                    "name": "Budget",
                    "purpose": "Track budget.",
                    "sheet_type": "summary",
                    "columns": "ABC",
                    "table_regions": "A1:C10",
                    "formula_regions": "E2:E10",
                    "chart_regions": "H2:M16",
                    "validation_rules": "required_headers",
                }
            ],
            "batches": [
                {
                    "index": 0,
                    "sheet_start": 1,
                    "sheet_end": 1,
                    "sheet_names": "Budget",
                }
            ],
        },
        goal="生成预算表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
    )

    assert issues == []
    assert plan["sheets"][0]["columns"] == []
    assert plan["sheets"][0]["table_regions"] == []
    assert plan["sheets"][0]["formula_regions"] == []
    assert plan["sheets"][0]["chart_regions"] == []
    assert plan["sheets"][0]["validation_rules"] == []
    assert plan["batches"] == [
        {
            "index": 0,
            "sheet_start": 1,
            "sheet_end": 1,
            "sheet_names": ["Budget"],
            "slide_start": 1,
            "slide_end": 1,
            "slide_titles": ["Budget"],
            "slide_roles": ["summary"],
        }
    ]


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


def test_ppt_strategy_build_plan_uses_reference_slide_names() -> None:
    from agent.domains.office.strategies.ppt import PptStrategy

    plan = PptStrategy().build_plan(
        goal="按参考案例生成 6 页产品介绍",
        requested_slide_count=6,
        build_batch_size=2,
        default_create_file="product-intro.pptx",
        merged_constraints={
            "reference_structure_constraints": {"units": [{"name": "封面"}, {"name": "问题"}, {"name": "方案"}]},
            "reference_style_constraints": {"style_tokens": {"theme": "blue"}},
        },
    )

    assert plan["slides"][0]["title"] == "封面"
    assert plan["slides"][1]["title"] == "问题"


@pytest.mark.asyncio
async def test_ppt_quality_report_can_record_reference_deviation() -> None:
    from agent.domains.office.workflow import qa_fix_node

    state = {
        "format": "pptx",
        "operation": "create",
        "write_required": True,
        "qa_fix_round": 0,
        "max_qa_fix_rounds": 2,
        "fidelity_deviations": [{"kind": "reference_style_deviation", "message": "theme fallback"}],
        "intermediate_results": [
            {
                "output": """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"deck.pptx","format":"pptx","role":"primary"}],"stats":{"slide_count":6,"content_slide_count":4,"notes_slide_count":4,"transition_slide_count":5,"visual_slide_count":4,"text_only_slide_count":0,"layout_variety_count":3,"picture_count":1,"chart_count":1,"table_count":0,"qa_checks":["view_stats","view_annotated","validate"]}}
```"""
            }
        ],
    }

    result = await qa_fix_node(state)

    assert result["current_stage"] == "finalize"
    assert result["quality_report"]["status"] == "passed"
    assert result["quality_report"]["fidelity_deviations"] == [
        {"kind": "reference_style_deviation", "message": "theme fallback"}
    ]


async def _run_build_stage_with_captured_context(
    state: dict[str, Any],
    *,
    strategy: Any,
    format_specific_guidance: str = "",
    gate: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from agent.domains.office.core.build import run_build_stage

    captured: dict[str, Any] = {}

    def fake_create_deep_agent(**kwargs: Any) -> object:
        captured["system_prompt"] = str(kwargs["system_prompt"])
        return object()

    async def fake_stream_nested_graph(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = _args[1] if len(_args) > 1 else kwargs.get("inputs", {})
        config = kwargs.get("config", {})
        messages = list(payload.get("messages", []) or []) if isinstance(payload, dict) else []
        captured["input_msg"] = str(getattr(messages[0], "content", "") or "") if messages else ""
        captured["config"] = config
        gate(captured["input_msg"], config)
        return {"messages": [AIMessage(content="build complete")]}

    with (
        patch("agent.domains.office.core.build.get_config", return_value={"configurable": {}}),
        patch("agent.domains.office.core.build.resolve_deepagents_runtime", return_value=([], object())),
        patch("agent.domains.office.core.build.build_chat_model", return_value=object()),
        patch("agent.domains.office.core.build.build_officecli_skill_bundle", return_value="officecli skill bundle"),
        patch("agent.domains.office.core.build.create_deep_agent", side_effect=fake_create_deep_agent),
        patch("agent.domains.office.core.build.stream_nested_graph", new=AsyncMock(side_effect=fake_stream_nested_graph)),
    ):
        result = await run_build_stage(
            state,
            strategy=strategy,
            system_template="{format_hint}{operation}{runtime_target}{default_create_file}{source_files_block}{format_specific_guidance}{phase_guidance}{skill_content}",
            format_specific_guidance=format_specific_guidance,
            office_model_role="orchestrator",
            subagents=[],
        )

    return result, captured


@pytest.mark.asyncio
async def test_ppt_reference_edit_build_stage_surfaces_goal_sources_batch_context_and_stage_transition() -> None:
    from agent.domains.office.strategies.ppt import PptStrategy

    goal = "参考品牌案例的视觉风格，更新季度复盘 PPT，并突出 AI 自动化带来的 ROI。"
    target_file = "/Users/test/Downloads/qbr-edit.pptx"
    reference_file = "/Users/test/Downloads/reference-style.pptx"
    strategy = PptStrategy()
    deck_plan = strategy.build_plan(
        goal=goal,
        requested_slide_count=4,
        build_batch_size=2,
        default_create_file="qbr-edit.pptx",
        merged_constraints={
            "reference_structure_constraints": {
                "units": [{"name": "封面"}, {"name": "ROI 机会"}, {"name": "执行路径"}]
            }
        },
    )
    state = {
        "goal": goal,
        "task_id": "ppt_reference_edit_build",
        "format": "pptx",
        "operation": "edit",
        "runtime_target_hint": "desktop",
        "default_create_file": "qbr-edit.pptx",
        "requested_slide_count": 4,
        "build_batch_size": 2,
        "deck_plan": deck_plan,
        "current_batch_index": 0,
        "completed_pages": 0,
        "allowed_source_files": [target_file, reference_file],
        "repair_mode": False,
        "inner_recursion_limit": 12,
        "intermediate_results": [],
        "evaluations": [],
        "cost_ledger": {"task_id": "ppt_reference_edit_build", "domain": "office"},
    }

    def gate(input_msg: str, config: dict[str, Any]) -> None:
        office_constraints = dict(config.get("configurable", {}).get("office_constraints") or {})
        assert office_constraints.get("allowed_source_files") == [target_file, reference_file]
        assert office_constraints.get("runtime_target") == "desktop"
        assert office_constraints.get("default_create_file") == "qbr-edit.pptx"
        assert "- operation: edit" in input_msg
        assert "- current_batch_index: 0" in input_msg
        assert "- current_batch_slide_range: 1-2" in input_msg
        assert "- current_batch_slide_titles: 封面, ROI 机会" in input_msg

    result, captured = await _run_build_stage_with_captured_context(state, strategy=strategy, gate=gate)

    phase_guidance = captured["system_prompt"]
    assert "- 当前阶段: build" in phase_guidance
    assert "- 只处理 slide 1-2。" in phase_guidance
    assert result["current_stage"] == "build"
    assert result["current_batch_index"] == 1


@pytest.mark.asyncio
async def test_xlsx_reference_create_build_stage_carries_planned_workbook_topology_into_execution_context() -> None:
    from agent.domains.office.workflow import planning_node
    from agent.domains.office.strategies.xlsx import XlsxStrategy

    goal = "参考财务模板创建预算工作簿，包含 Inputs、Calculations、Dashboard 三张表。"
    planning_result = await planning_node(
        {
            "goal": goal,
            "format": "xlsx",
            "operation": "create",
            "requested_slide_count": 0,
            "build_batch_size": 2,
            "default_create_file": "budget-model.xlsx",
            "goal_constraints": {
                "hard_requirements": ["Inputs", "Calculations", "Dashboard", "preserve formulas"]
            },
            "reference_structure_constraints": {
                "units": [{"name": "Inputs"}, {"name": "Dashboard"}, {"name": "preserve formulas and archive notes"}]
            },
            "reference_style_constraints": {"style_tokens": {"summary_position": "top"}},
            "cost_ledger": {},
        }
    )
    strategy = XlsxStrategy()
    state = {
        "goal": goal,
        "task_id": "xlsx_reference_create_build",
        "format": "xlsx",
        "operation": "create",
        "runtime_target_hint": "desktop",
        "default_create_file": planning_result["task_profile"]["target_filename"],
        "requested_slide_count": 0,
        "build_batch_size": 2,
        "deck_plan": planning_result["deck_plan"],
        "task_profile": planning_result["task_profile"],
        "current_batch_index": 0,
        "completed_pages": 0,
        "allowed_source_files": ["/Users/test/Downloads/finance-template.xlsx"],
        "repair_mode": False,
        "inner_recursion_limit": 12,
        "intermediate_results": [],
        "evaluations": [],
        "cost_ledger": {"task_id": "xlsx_reference_create_build", "domain": "office"},
    }

    def gate(input_msg: str, config: dict[str, Any]) -> None:
        office_constraints = dict(config.get("configurable", {}).get("office_constraints") or {})
        assert office_constraints.get("allowed_source_files") == ["/Users/test/Downloads/finance-template.xlsx"]
        assert "- workbook_plan:" in input_msg
        assert "sheet[1] Inputs (worksheet)" in input_msg
        assert "sheet[2] Calculations (worksheet)" in input_msg
        assert "sheet[3] Dashboard (dashboard)" in input_msg
        assert "- current_batch_sheet_range: 1-2" in input_msg

    result, captured = await _run_build_stage_with_captured_context(state, strategy=strategy, gate=gate)

    phase_guidance = captured["system_prompt"]
    assert "- 当前阶段: build" in phase_guidance
    assert "- 只处理 sheet 1-2。" in phase_guidance
    assert result["current_stage"] == "build"
    assert result["current_batch_index"] == 1


@pytest.mark.asyncio
async def test_docx_protected_section_build_stage_surfaces_target_and_protected_sections_into_execution_context() -> None:
    from agent.domains.office.strategies.docx import DocxStrategy
    from agent.domains.office.workflow import planning_node

    class FakeLLM:
        async def ainvoke(self, _messages):
            return AIMessage(
                content='{"section_headings":["执行摘要","实施计划"],"formatting_instructions":["preserve numbering"]}'
            )

    with patch("agent.domains.office.workflow.get_llm", return_value=FakeLLM()):
        planning_result = await planning_node(
            {
                "goal": "参考方案模板，更新项目方案中的执行摘要和实施计划，并保留附录与致谢。",
                "format": "docx",
                "operation": "edit",
                "requested_slide_count": 0,
                "build_batch_size": 1,
                "default_create_file": "project-plan.docx",
                "goal_constraints": {
                    "hard_requirements": ["执行摘要", "实施计划", "preserve numbering"],
                    "section_headings": ["执行摘要", "实施计划"],
                    "formatting_instructions": ["preserve numbering"],
                },
                "reference_structure_constraints": {
                    "units": [{"name": "执行摘要"}, {"name": "实施计划"}, {"name": "附录"}]
                },
                "reference_style_constraints": {"style_tokens": {"heading_style": "Heading1"}},
                "existing_document_profile": {
                    "units": ["执行摘要", "实施计划", "附录", "致谢"],
                    "protected_units": ["附录", "致谢"],
                },
                "cost_ledger": {},
            }
        )

    strategy = DocxStrategy()
    state = {
        "goal": "参考方案模板，更新项目方案中的执行摘要和实施计划，并保留附录与致谢。",
        "task_id": "docx_protected_section_build",
        "format": "docx",
        "operation": "edit",
        "runtime_target_hint": "desktop",
        "default_create_file": planning_result["task_profile"]["target_filename"],
        "requested_slide_count": 0,
        "build_batch_size": 1,
        "deck_plan": planning_result["deck_plan"],
        "task_profile": planning_result["task_profile"],
        "current_batch_index": 0,
        "completed_pages": 0,
        "allowed_source_files": ["/Users/test/Downloads/project-plan.docx"],
        "repair_mode": False,
        "inner_recursion_limit": 12,
        "intermediate_results": [],
        "evaluations": [],
        "cost_ledger": {"task_id": "docx_protected_section_build", "domain": "office"},
    }

    def gate(input_msg: str, config: dict[str, Any]) -> None:
        office_constraints = dict(config.get("configurable", {}).get("office_constraints") or {})
        assert office_constraints.get("allowed_source_files") == ["/Users/test/Downloads/project-plan.docx"]
        assert "- operation: edit" in input_msg
        assert "- target_sections: 执行摘要, 实施计划" in input_msg
        assert "- protected_sections: 附录, 致谢" in input_msg

    result, captured = await _run_build_stage_with_captured_context(state, strategy=strategy, gate=gate)

    phase_guidance = captured["system_prompt"]
    assert "- 当前阶段: build" in phase_guidance
    assert result["current_stage"] == "qa_fix"
    assert result["current_batch_index"] == 1


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
async def test_office_domain_xlsx_finalize_counts_sheet_count_as_completed_pages() -> None:
    from agent.domains.office.orchestrated import run_office_domain_orchestrated

    payload = """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"budget.xlsx","format":"xlsx","role":"primary"}],"stats":{"sheet_count":3}}
```"""

    with (
        patch(
            "agent.domains.office.orchestrated.stream_nested_graph",
            new=AsyncMock(return_value={"final_result": payload, "step_history": [{"strategy": "sequential"}]}),
        ),
        patch("agent.domains.office.orchestrated.infer_office_runtime_target", return_value="desktop"),
        patch(
            "agent.domains.office.orchestrated.execute_officecli_spec",
            new=AsyncMock(return_value={"success": True, "message": "Closing resident.", "command": "officecli close budget.xlsx"}),
        ),
    ):
        result = await run_office_domain_orchestrated({"query": "做一个预算表", "task_id": "office_xlsx_finalize"})

    assert result.status == "ok"
    assert result.budget["cost_ledger"]["completed_pages"] == 3


@pytest.mark.asyncio
async def test_office_domain_docx_finalize_counts_section_count_as_completed_pages() -> None:
    from agent.domains.office.orchestrated import run_office_domain_orchestrated

    payload = """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"project-plan.docx","format":"docx","role":"primary"}],"stats":{"section_count":3}}
```"""

    with (
        patch(
            "agent.domains.office.orchestrated.stream_nested_graph",
            new=AsyncMock(return_value={"final_result": payload, "step_history": [{"strategy": "sequential"}]}),
        ),
        patch("agent.domains.office.orchestrated.infer_office_runtime_target", return_value="desktop"),
        patch(
            "agent.domains.office.orchestrated.execute_officecli_spec",
            new=AsyncMock(return_value={"success": True, "message": "Closing resident.", "command": "officecli close project-plan.docx"}),
        ),
    ):
        result = await run_office_domain_orchestrated({"query": "生成项目方案", "task_id": "office_docx_finalize"})

    assert result.status == "ok"
    assert result.budget["cost_ledger"]["completed_pages"] == 3
    assert result.review["quality_report_summary"]["section_count"] == 3


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
async def test_office_domain_result_surfaces_fidelity_deviation_summary() -> None:
    from agent.domains.office.orchestrated import run_office_domain_orchestrated

    quality_report = {
        "format": "pptx",
        "operation": "create",
        "validated": True,
        "status": "passed",
        "passed": True,
        "issue_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "artifact_count": 1,
        "summary": "done",
        "issues": [],
        "qa_fix_round": 0,
        "max_qa_fix_rounds": 2,
        "stats_summary": {"slide_count": 6},
        "fidelity_deviations": [{"kind": "reference_style_deviation", "message": "theme fallback"}],
    }
    payload = """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"deck.pptx","path":"/Users/test/Desktop/deck.pptx","format":"pptx","role":"primary"}],"stats":{"slide_count":6}}
```"""

    with (
        patch(
            "agent.domains.office.orchestrated.stream_nested_graph",
            new=AsyncMock(
                return_value={
                    "final_result": payload,
                    "quality_report": quality_report,
                    "cost_ledger": {"task_id": "office_fidelity", "domain": "office"},
                    "step_history": [{"strategy": "sequential"}],
                }
            ),
        ),
        patch("agent.domains.office.orchestrated.infer_office_runtime_target", return_value="desktop"),
        patch(
            "agent.domains.office.orchestrated.execute_officecli_spec",
            new=AsyncMock(return_value={"success": True, "message": "Closing resident.", "command": "officecli close /Users/test/Desktop/deck.pptx"}),
        ),
    ):
        result = await run_office_domain_orchestrated({"query": "做一个 PPT", "task_id": "office_fidelity"})

    assert result.status == "ok"
    assert result.review["quality_report_summary"]["fidelity_deviation_count"] == 1
    assert result.budget["quality_report_summary"]["fidelity_deviation_count"] == 1
    assert result.budget["cost_ledger"]["quality_report_summary"]["fidelity_deviation_count"] == 1


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
async def test_office_workflow_xlsx_quality_stats_required_for_write() -> None:
    from agent.domains.office.workflow import evaluate_node

    state = {
        "format": "xlsx",
        "operation": "create",
        "write_required": True,
        "intermediate_results": [
            {
                "output": """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"budget.xlsx","format":"xlsx","role":"primary"}],"stats":{}}
```"""
            }
        ],
    }

    result = await evaluate_node(state)

    assert result["evaluations"][0]["passed"] is False
    assert any("XLSX 写入结果缺少质量 stats" in issue["message"] for issue in result["evaluations"][0]["issues"])


@pytest.mark.asyncio
async def test_office_workflow_xlsx_quality_stats_require_sheet_count() -> None:
    from agent.domains.office.workflow import evaluate_node

    state = {
        "format": "xlsx",
        "operation": "create",
        "write_required": True,
        "intermediate_results": [
            {
                "output": """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"budget.xlsx","format":"xlsx","role":"primary"}],"stats":{"foo":"bar"}}
```"""
            }
        ],
    }

    result = await evaluate_node(state)

    assert result["evaluations"][0]["passed"] is False
    assert any("sheet_count" in issue["message"] for issue in result["evaluations"][0]["issues"])


@pytest.mark.asyncio
async def test_office_workflow_xlsx_quality_stats_pass_with_sheet_count() -> None:
    from agent.domains.office.workflow import evaluate_node

    state = {
        "format": "xlsx",
        "operation": "create",
        "write_required": True,
        "intermediate_results": [
            {
                "output": """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"budget.xlsx","format":"xlsx","role":"primary"}],"stats":{"sheet_count":3}}
```"""
            }
        ],
    }

    result = await evaluate_node(state)

    assert result["evaluations"][0]["passed"] is True
    assert result["quality_report"]["stats_summary"]["sheet_count"] == 3


@pytest.mark.asyncio
async def test_office_qa_fix_uses_sheet_count_when_slide_count_missing() -> None:
    from agent.domains.office.workflow import qa_fix_node

    state = {
        "format": "xlsx",
        "operation": "create",
        "write_required": True,
        "qa_fix_round": 0,
        "max_qa_fix_rounds": 2,
        "intermediate_results": [
            {
                "output": """```json
{"operation":"create","validated":true,"summary":"done","artifacts":[{"filename":"budget.xlsx","format":"xlsx","role":"primary"}],"stats":{"sheet_count":3}}
```"""
            }
        ],
    }

    result = await qa_fix_node(state)

    assert result["current_stage"] == "finalize"
    assert result["quality_report"]["stats_summary"]["sheet_count"] == 3
    assert result["cost_ledger"]["completed_pages"] == 3


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
