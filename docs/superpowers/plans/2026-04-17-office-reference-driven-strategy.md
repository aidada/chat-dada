# Office Reference-Driven Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a shared reference-understanding layer to the Office domain and use it to drive goal-first, reference-aligned `pptx`, `xlsx`, and `docx` create/edit workflows.

**Architecture:** Keep the existing staged `Office Core` pipeline (`planning -> build -> qa_fix -> finalize`), insert a shared `Reference Understanding Layer` ahead of format planning, then retrofit `PptStrategy` and add real `XlsxStrategy` / `DocxStrategy` implementations that consume merged goal and reference constraints. Use OfficeCLI `view/get/query` first, then structured mutations, then raw fallback only for narrow fidelity gaps.

**Tech Stack:** Python, LangGraph, OfficeCLI (`pptx/docx/xlsx`), pytest, existing `agent/workflows/office` strategy/core modules

---

## File Structure

### Shared Office reference layer

- Create: `agent/workflows/office/reference_models.py`
  - typed payload builders for `goal_constraints`, `reference_structure_constraints`, `reference_style_constraints`, `existing_document_profile`, `conflict_resolution`
- Create: `agent/workflows/office/reference_inspector.py`
  - OfficeCLI-backed read helpers for reference and existing files
- Create: `agent/workflows/office/reference_profiler.py`
  - format-aware profiling that turns inspect output into reusable constraints
- Create: `agent/workflows/office/reference_resolver.py`
  - merges goal, reference, and existing-document profiles into strategy-ready constraints

### Workflow integration

- Modify: `agent/workflows/office/core/state.py`
  - add `reference_files`, `goal_constraints`, `reference_*_constraints`, `existing_document_profile`, `fidelity_deviations`
- Modify: `agent/workflows/office/goal_normalizer.py`
  - normalize reference file inputs and operation-specific planner hints
- Modify: `agent/workflows/office/workflow.py`
  - add reference-aware planning step inputs and planner summary propagation
- Modify: `agent/workflows/office/core/build.py`
  - thread merged constraints and fidelity hints into build execution
- Modify: `agent/workflows/office/core/qa.py`
  - add fidelity deviation reporting to `quality_report`
- Modify: `agent/workflows/office/orchestrated.py`
  - carry reference metadata into final `review` and `budget`

### Strategy layer

- Modify: `agent/workflows/office/strategies/base.py`
  - add reference-aware strategy interfaces
- Modify: `agent/workflows/office/strategies/ppt.py`
  - consume merged constraints and retrofit reference fidelity
- Modify: `agent/workflows/office/strategies/xlsx.py`
  - replace placeholder with workbook/sheet strategy
- Modify: `agent/workflows/office/strategies/docx.py`
  - replace placeholder with document/section strategy
- Modify: `agent/workflows/office/strategies/__init__.py`
  - keep selector stable while upgrading concrete strategies

### Runtime / diagnostics

- Modify: `agent/runtime/cost_logging.py`
  - record reference coverage and fidelity deviations in the ledger summary
- Modify: `agent/runtime/task_execution.py`
  - surface reference-driven partial progress and fidelity diagnostics

### Tests

- Create: `tests/test_office_reference_models.py`
- Create: `tests/test_office_reference_layer.py`
- Modify: `tests/test_office_domain.py`
- Modify: `tests/test_cost_logging.py`
- Modify: `tests/test_shared_officecli.py`

## Execution Order

1. Shared reference data model and profilers
2. Workflow integration for staged planning/build/qa
3. PPT retrofit
4. XLSX strategy
5. DOCX strategy
6. Diagnostics, fidelity summaries, and regression coverage

## Task 1: Add Shared Reference Data Models

**Files:**
- Create: `agent/workflows/office/reference_models.py`
- Modify: `agent/workflows/office/core/state.py`
- Test: `tests/test_office_reference_models.py`

- [ ] **Step 1: Write the failing tests for normalized reference payloads**

```python
from agent.workflows.office.reference_models import (
    build_conflict_resolution,
    build_existing_document_profile,
    build_goal_constraints,
    build_reference_style_constraints,
    build_reference_structure_constraints,
)


def test_build_goal_constraints_goal_wins_over_reference() -> None:
    payload = build_goal_constraints(
        format_name="xlsx",
        operation="edit",
        goal="按用户规范修改预算表",
        hard_requirements=["preserve formulas", "rename summary sheet"],
    )

    assert payload["format"] == "xlsx"
    assert payload["operation"] == "edit"
    assert payload["hard_requirements"] == ["preserve formulas", "rename summary sheet"]


def test_build_conflict_resolution_defaults_to_goal_first() -> None:
    resolution = build_conflict_resolution()

    assert resolution["priority_order"] == ["goal", "reference"]
    assert resolution["record_deviations"] is True


def test_build_existing_document_profile_tracks_protected_units() -> None:
    profile = build_existing_document_profile(
        format_name="docx",
        units=[{"name": "Executive Summary"}],
        protected_units=["Appendix"],
    )

    assert profile["format"] == "docx"
    assert profile["protected_units"] == ["Appendix"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_reference_models.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.workflows.office.reference_models'`

- [ ] **Step 3: Write minimal reference model builders**

```python
# agent/workflows/office/reference_models.py
from __future__ import annotations

from typing import Any


def build_goal_constraints(*, format_name: str, operation: str, goal: str, hard_requirements: list[str] | None = None) -> dict[str, Any]:
    return {
        "format": str(format_name or "").lower(),
        "operation": str(operation or "").lower(),
        "goal": str(goal or "").strip(),
        "hard_requirements": list(hard_requirements or []),
    }


def build_reference_structure_constraints(*, format_name: str, units: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "format": str(format_name or "").lower(),
        "units": list(units or []),
    }


def build_reference_style_constraints(*, format_name: str, style_tokens: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "format": str(format_name or "").lower(),
        "style_tokens": dict(style_tokens or {}),
    }


def build_existing_document_profile(*, format_name: str, units: list[dict[str, Any]] | None = None, protected_units: list[str] | None = None) -> dict[str, Any]:
    return {
        "format": str(format_name or "").lower(),
        "units": list(units or []),
        "protected_units": list(protected_units or []),
    }


def build_conflict_resolution() -> dict[str, Any]:
    return {
        "priority_order": ["goal", "reference"],
        "record_deviations": True,
    }
```

- [ ] **Step 4: Extend Office workflow state with reference fields**

```python
# agent/workflows/office/core/state.py
class OfficeWorkflowState(TypedDict, total=False):
    reference_files: list[str]
    goal_constraints: dict[str, Any]
    reference_structure_constraints: dict[str, Any]
    reference_style_constraints: dict[str, Any]
    existing_document_profile: dict[str, Any]
    fidelity_deviations: list[dict[str, Any]]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_office_reference_models.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/reference_models.py agent/workflows/office/core/state.py tests/test_office_reference_models.py
git commit -m "feat: add office reference constraint models"
```

## Task 2: Build ReferenceInspector, ReferenceProfiler, and ConstraintResolver

**Files:**
- Create: `agent/workflows/office/reference_inspector.py`
- Create: `agent/workflows/office/reference_profiler.py`
- Create: `agent/workflows/office/reference_resolver.py`
- Test: `tests/test_office_reference_layer.py`

- [ ] **Step 1: Write the failing tests for reference inspection and resolution**

```python
from agent.workflows.office.reference_profiler import profile_reference_payload
from agent.workflows.office.reference_resolver import resolve_reference_constraints


def test_profile_reference_payload_for_ppt_extracts_structure_and_style() -> None:
    profiled = profile_reference_payload(
        format_name="pptx",
        inspect_payload={
            "outline": [{"title": "Intro"}, {"title": "Plan"}],
            "stats": {"slide_count": 2, "layout_variety_count": 2},
        },
    )

    assert profiled["structure"]["units"][0]["name"] == "Intro"
    assert profiled["style"]["style_tokens"]["slide_count"] == 2


def test_resolve_reference_constraints_keeps_goal_first() -> None:
    merged = resolve_reference_constraints(
        goal_constraints={"hard_requirements": ["rename summary sheet"]},
        reference_structure_constraints={"units": [{"name": "Summary"}]},
        reference_style_constraints={"style_tokens": {"theme": "blue"}},
        existing_document_profile={"protected_units": ["RawData"]},
    )

    assert merged["goal_constraints"]["hard_requirements"] == ["rename summary sheet"]
    assert merged["conflict_resolution"]["priority_order"] == ["goal", "reference"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_reference_layer.py -q`
Expected: FAIL with import errors for the new modules

- [ ] **Step 3: Add minimal inspection wrappers around OfficeCLI reads**

```python
# agent/workflows/office/reference_inspector.py
from __future__ import annotations

from typing import Any

from agent.tools.officecli import execute_officecli_spec


async def inspect_reference_file(*, format_name: str, file_path: str) -> dict[str, Any]:
    if format_name == "pptx":
        outline = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "outline"})
        stats = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "stats"})
        return {"outline": outline, "stats": stats}
    if format_name == "xlsx":
        text = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "text"})
        issues = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "issues"})
        return {"text": text, "issues": issues}
    text = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "text"})
    annotated = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "annotated"})
    return {"text": text, "annotated": annotated}
```

- [ ] **Step 4: Add structured profiling and merging**

```python
# agent/workflows/office/reference_profiler.py
from __future__ import annotations

from typing import Any


def profile_reference_payload(*, format_name: str, inspect_payload: dict[str, Any]) -> dict[str, Any]:
    if format_name == "pptx":
        outline = inspect_payload.get("outline", [])
        stats = inspect_payload.get("stats", {})
        return {
            "structure": {"units": [{"name": str(item.get("title", "") or "")} for item in outline]},
            "style": {"style_tokens": {"slide_count": int(stats.get("slide_count", 0) or 0)}},
        }
    return {
        "structure": {"units": []},
        "style": {"style_tokens": {}},
    }


# agent/workflows/office/reference_resolver.py
from __future__ import annotations

from typing import Any

from agent.workflows.office.reference_models import build_conflict_resolution


def resolve_reference_constraints(
    *,
    goal_constraints: dict[str, Any],
    reference_structure_constraints: dict[str, Any],
    reference_style_constraints: dict[str, Any],
    existing_document_profile: dict[str, Any],
) -> dict[str, Any]:
    return {
        "goal_constraints": dict(goal_constraints or {}),
        "reference_structure_constraints": dict(reference_structure_constraints or {}),
        "reference_style_constraints": dict(reference_style_constraints or {}),
        "existing_document_profile": dict(existing_document_profile or {}),
        "conflict_resolution": build_conflict_resolution(),
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_office_reference_layer.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/reference_inspector.py agent/workflows/office/reference_profiler.py agent/workflows/office/reference_resolver.py tests/test_office_reference_layer.py
git commit -m "feat: add office reference inspection and resolution layer"
```

## Task 3: Integrate Reference Constraints Into Office Workflow Planning

**Files:**
- Modify: `agent/workflows/office/goal_normalizer.py`
- Modify: `agent/workflows/office/workflow.py`
- Modify: `agent/workflows/office/strategies/base.py`
- Modify: `agent/workflows/office/strategies/default.py`
- Test: `tests/test_office_domain.py`

- [ ] **Step 1: Write the failing workflow test for reference-aware planning**

```python
import pytest


@pytest.mark.asyncio
async def test_planning_node_carries_reference_constraints_into_task_profile() -> None:
    from agent.workflows.office.workflow import planning_node

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
            "reference_structure_constraints": {"units": [{"name": "封面"}]},
            "reference_style_constraints": {"style_tokens": {"theme": "blue"}},
            "cost_ledger": {},
        }
    )

    assert result["task_profile"]["target_filename"].endswith(".pptx")
    assert result["planning_summary"]["slide_count"] == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py::test_planning_node_carries_reference_constraints_into_task_profile -q`
Expected: FAIL because reference-aware planner inputs are ignored

- [ ] **Step 3: Normalize reference files and goal constraints in preflight**

```python
# agent/workflows/office/goal_normalizer.py
def normalize_goal_profile(
    *,
    goal: str,
    file_hint: str,
    source_files: list[str],
    explicit_format: str,
    explicit_operation: str,
    reference_files: list[str] | None = None,
) -> dict[str, Any]:
    format_name = infer_format(goal, file_hint, source_files, explicit_format)
    operation = infer_operation(goal, source_files, explicit_operation)
    return {
        "format": format_name,
        "operation": operation,
        "reference_files": [str(item).strip() for item in reference_files or [] if str(item).strip()],
    }
```

```python
# agent/workflows/office/workflow.py
return {
    "format": format_name,
    "operation": operation,
    "task_profile": {
        "format": format_name,
        "operation": operation,
        "target_filename": default_create_file,
        "source_files": source_files,
        "reference_files": list(normalized.get("reference_files") or []),
        "runtime_target": runtime_target,
        "quality_profile": quality_profile,
    },
}
```

- [ ] **Step 4: Make strategy contracts accept merged constraints**

```python
# agent/workflows/office/strategies/base.py
class OfficeFormatStrategy(Protocol):
    def build_plan(
        self,
        *,
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError
```

```python
# agent/workflows/office/strategies/default.py
def build_plan(
    self,
    *,
    goal: str,
    requested_slide_count: int,
    build_batch_size: int,
    default_create_file: str,
    merged_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    title = str(goal or "").replace("\n", " ").strip()[:80] or str(default_create_file or "").rsplit(".", 1)[0] or "Office task"
    return {
        "title": title,
        "slide_count": 0,
        "slides": [],
        "batches": [
            {
                "index": 0,
                "slide_start": 0,
                "slide_end": 0,
                "slide_titles": [title],
                "slide_roles": ["document"],
            }
        ],
    }
```

- [ ] **Step 5: Update planning node to pass merged constraints**

```python
# agent/workflows/office/workflow.py
merged_constraints = {
    "goal_constraints": dict(state.get("goal_constraints") or {}),
    "reference_structure_constraints": dict(state.get("reference_structure_constraints") or {}),
    "reference_style_constraints": dict(state.get("reference_style_constraints") or {}),
    "existing_document_profile": dict(state.get("existing_document_profile") or {}),
}
raw_plan = strategy.build_plan(
    goal=str(state.get("goal", "") or ""),
    requested_slide_count=requested_slide_count or 6,
    build_batch_size=build_batch_size,
    default_create_file=default_create_file,
    merged_constraints=merged_constraints,
)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_office_domain.py::test_planning_node_carries_reference_constraints_into_task_profile -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add agent/workflows/office/goal_normalizer.py agent/workflows/office/workflow.py agent/workflows/office/strategies/base.py agent/workflows/office/strategies/default.py tests/test_office_domain.py
git commit -m "feat: thread reference constraints through office planning"
```

## Task 4: Retrofit PptStrategy To Consume Reference Constraints

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py`
- Modify: `agent/workflows/office/core/build.py`
- Modify: `agent/workflows/office/core/qa.py`
- Test: `tests/test_office_domain.py`

- [ ] **Step 1: Write the failing tests for PPT reference-aligned planning and QA**

```python
def test_ppt_strategy_build_plan_uses_reference_slide_names() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy

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


def test_ppt_quality_report_can_record_reference_deviation() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy

    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats={"slide_count": 6, "content_slide_count": 4, "notes_slide_count": 4, "transition_slide_count": 5, "visual_slide_count": 4, "text_only_slide_count": 0, "layout_variety_count": 3, "picture_count": 1, "chart_count": 1, "table_count": 0, "qa_checks": ["view_stats", "view_annotated", "validate"]},
    )

    assert issues == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py::test_ppt_strategy_build_plan_uses_reference_slide_names -q`
Expected: FAIL because `PptStrategy.build_plan()` does not accept or use `merged_constraints`

- [ ] **Step 3: Use reference structure and style tokens in PPT planning**

```python
# agent/workflows/office/strategies/ppt.py
def build_plan(
    self,
    *,
    goal: str,
    requested_slide_count: int,
    build_batch_size: int,
    default_create_file: str,
    merged_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = dict(merged_constraints or {})
    reference_units = list(dict(merged.get("reference_structure_constraints") or {}).get("units") or [])
    slide_titles = [str(item.get("name", "") or "").strip() for item in reference_units if str(item.get("name", "") or "").strip()]
    if len(slide_titles) < slide_count:
        slide_titles.extend(_build_slide_titles(slide_count)[len(slide_titles):])
    slides = [
        {
            "index": idx + 1,
            "title": slide_titles[idx],
            "role": _slide_role(idx + 1, slide_count),
            "section": _slide_section(idx + 1, slide_count),
            "takeaway": _slide_takeaway(slide_titles[idx], idx + 1, slide_count),
            "layout_type": _slide_layout_type(idx + 1, slide_count),
            "visual_requirements": _slide_visual_requirements(idx + 1, slide_count),
            "transition_required": idx + 1 > 1,
            "notes_required": idx + 1 not in {1, slide_count},
        }
        for idx in range(slide_count)
    ]
    return {
        "title": _infer_deck_title(goal, default_create_file),
        "slide_count": slide_count,
        "slides": slides,
        "batches": [],
    }
```

- [ ] **Step 4: Thread fidelity hints into build and QA**

```python
# agent/workflows/office/core/build.py
input_msg = "\n".join(
    strategy.build_input_sections(
        goal=str(state.get("goal", "") or ""),
        operation=operation,
        format_hint=format_hint,
        runtime_target=runtime_target,
        default_create_file=default_create_file,
        requested_slide_count=requested_slide_count,
        build_batch_size=build_batch_size,
        source_files=source_files,
        context=context,
        qa_feedback=qa_feedback,
        plan=deck_plan,
        current_batch_index=current_batch_index,
        repair_mode=repair_mode,
    )
)
```

```python
# agent/workflows/office/core/qa.py
quality_report = build_quality_report(
    format_name=str(state.get("format", "") or state.get("format_hint", "") or ""),
    operation=operation,
    validated=validated,
    artifacts=list(artifacts or []),
    summary=summary,
    stats=stats,
    issues=issues,
    qa_fix_round=int(state.get("qa_fix_round", 0) or 0),
    max_qa_fix_rounds=int(state.get("max_qa_fix_rounds", 0) or 0),
    terminal_reason=str(state.get("terminal_reason", "") or ""),
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_office_domain.py -q`
Expected: PASS for existing PPT tests and new reference-aware PPT tests

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py agent/workflows/office/core/build.py agent/workflows/office/core/qa.py tests/test_office_domain.py
git commit -m "feat: retrofit ppt strategy for reference-driven planning"
```

## Task 5: Replace XlsxStrategy Placeholder With WorkbookPlanner and SheetBuilder

**Files:**
- Modify: `agent/workflows/office/strategies/xlsx.py`
- Modify: `agent/workflows/office/strategies/__init__.py`
- Modify: `agent/workflows/office/core/qa.py`
- Test: `tests/test_office_domain.py`

- [ ] **Step 1: Write the failing tests for workbook planning**

```python
def test_xlsx_strategy_builds_sheet_plan_from_goal_and_reference() -> None:
    from agent.workflows.office.strategies.xlsx import XlsxStrategy

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py::test_xlsx_strategy_builds_sheet_plan_from_goal_and_reference -q`
Expected: FAIL because `XlsxStrategy` is still a placeholder

- [ ] **Step 3: Implement minimal workbook planner and builder helpers**

```python
# agent/workflows/office/strategies/xlsx.py
from __future__ import annotations

from typing import Any


class XlsxStrategy:
    def build_plan(
        self,
        *,
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(merged_constraints or {})
        required = list(dict(merged.get("goal_constraints") or {}).get("hard_requirements") or [])
        sheet_names = required or ["Sheet1"]
        sheets = []
        for name in sheet_names:
            lowered = str(name).lower()
            sheet_type = "dashboard" if "dashboard" in lowered else "summary" if "summary" in lowered else "raw_data"
            sheets.append(
                {
                    "name": str(name),
                    "purpose": str(name),
                    "sheet_type": sheet_type,
                    "columns": [],
                    "table_regions": [],
                    "formula_regions": [],
                    "chart_regions": [],
                    "validation_rules": [],
                }
            )
        return {
            "title": str(default_create_file or "Workbook"),
            "sheet_count": len(sheets),
            "sheets": sheets,
            "batches": [{"index": idx, "sheet_names": [sheet["name"]], "objective": sheet["purpose"]} for idx, sheet in enumerate(sheets)],
        }
```

- [ ] **Step 4: Add XLSX-specific quality checks**

```python
# agent/workflows/office/strategies/xlsx.py
def evaluate_quality_stats(self, *, operation: str, stats: dict[str, Any]) -> list[dict[str, Any]]:
    if operation not in {"create", "edit", "transform"}:
        return []
    if not stats:
        return [{"severity": "error", "message": "XLSX 缺少 workbook stats"}]
    return []
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_office_domain.py -q`
Expected: PASS for new XLSX tests and selector compatibility tests

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/strategies/xlsx.py agent/workflows/office/strategies/__init__.py agent/workflows/office/core/qa.py tests/test_office_domain.py
git commit -m "feat: add xlsx reference-driven strategy"
```

## Task 6: Replace DocxStrategy Placeholder With DocumentPlanner and SectionWriter

**Files:**
- Modify: `agent/workflows/office/strategies/docx.py`
- Modify: `agent/workflows/office/core/qa.py`
- Test: `tests/test_office_domain.py`

- [ ] **Step 1: Write the failing tests for DOCX planning**

```python
def test_docx_strategy_builds_section_plan_from_goal_and_reference() -> None:
    from agent.workflows.office.strategies.docx import DocxStrategy

    plan = DocxStrategy().build_plan(
        goal="生成一份项目方案，包含背景、目标、实施计划、风险控制",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="project-plan.docx",
        merged_constraints={
            "goal_constraints": {"hard_requirements": ["背景", "目标", "实施计划", "风险控制"]},
            "reference_structure_constraints": {"units": [{"name": "背景"}, {"name": "目标"}]},
            "reference_style_constraints": {"style_tokens": {"heading_style": "Heading1"}},
        },
    )

    assert plan["section_count"] == 4
    assert plan["sections"][0]["heading"] == "背景"
    assert plan["sections"][2]["content_mode"] == "mixed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py::test_docx_strategy_builds_section_plan_from_goal_and_reference -q`
Expected: FAIL because `DocxStrategy` is still a placeholder

- [ ] **Step 3: Implement minimal document planner**

```python
# agent/workflows/office/strategies/docx.py
from __future__ import annotations

from typing import Any


class DocxStrategy:
    def build_plan(
        self,
        *,
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(merged_constraints or {})
        required = list(dict(merged.get("goal_constraints") or {}).get("hard_requirements") or [])
        headings = required or ["正文"]
        sections = [
            {
                "index": idx + 1,
                "heading": str(name),
                "purpose": str(name),
                "key_points": [],
                "content_mode": "mixed",
                "style_requirements": dict(dict(merged.get("reference_style_constraints") or {}).get("style_tokens") or {}),
            }
            for idx, name in enumerate(headings)
        ]
        return {
            "title": str(default_create_file or "Document"),
            "section_count": len(sections),
            "sections": sections,
            "batches": [{"index": idx, "section_names": [section["heading"]], "objective": section["purpose"]} for idx, section in enumerate(sections)],
        }
```

- [ ] **Step 4: Add DOCX-specific QA checks**

```python
# agent/workflows/office/strategies/docx.py
def evaluate_quality_stats(self, *, operation: str, stats: dict[str, Any]) -> list[dict[str, Any]]:
    if operation not in {"create", "edit", "transform"}:
        return []
    if not stats:
        return [{"severity": "error", "message": "DOCX 缺少 document stats"}]
    return []
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_office_domain.py -q`
Expected: PASS for new DOCX tests and selector compatibility tests

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/strategies/docx.py agent/workflows/office/core/qa.py tests/test_office_domain.py
git commit -m "feat: add docx reference-driven strategy"
```

## Task 7: Add Fidelity Diagnostics To QA, Result Summaries, and Cost Ledger

**Files:**
- Modify: `agent/workflows/office/core/quality_report.py`
- Modify: `agent/workflows/office/orchestrated.py`
- Modify: `agent/runtime/cost_logging.py`
- Modify: `agent/runtime/task_execution.py`
- Modify: `tests/test_cost_logging.py`
- Modify: `tests/test_office_domain.py`

- [ ] **Step 1: Write the failing tests for fidelity summaries**

```python
from agent.workflows.office.core.quality_report import build_quality_report, summarize_quality_report


def test_quality_report_summary_includes_fidelity_deviations() -> None:
    report = build_quality_report(
        format_name="pptx",
        operation="create",
        validated=True,
        artifacts=[{"name": "deck.pptx"}],
        summary="done",
        stats={"slide_count": 6},
        issues=[],
        qa_fix_round=0,
        max_qa_fix_rounds=2,
        terminal_reason="",
    )
    report["fidelity_deviations"] = [{"kind": "style_deviation", "message": "theme fallback"}]

    summary = summarize_quality_report(report)

    assert summary["fidelity_deviation_count"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cost_logging.py::test_quality_report_summary_includes_fidelity_deviations -q`
Expected: FAIL because fidelity deviations are not summarized

- [ ] **Step 3: Extend quality reports and ledger summaries**

```python
# agent/workflows/office/core/quality_report.py
def summarize_quality_report(report: dict[str, Any] | None) -> dict[str, Any]:
    active = dict(report or {})
    summary = {
        "status": str(active.get("status", "") or ""),
        "passed": bool(active.get("passed", False)),
        "issue_count": int(active.get("issue_count", 0) or 0),
        "error_count": int(active.get("error_count", 0) or 0),
        "warning_count": int(active.get("warning_count", 0) or 0),
        "validated": bool(active.get("validated", False)),
    }
    summary["fidelity_deviation_count"] = len(list(active.get("fidelity_deviations") or []))
    return summary
```

```python
# agent/runtime/cost_logging.py
def attach_quality_summary(
    ledger: dict[str, Any] | None,
    *,
    quality_report_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    active = dict(ledger or {})
    if quality_report_summary:
        active["quality_report_summary"] = dict(quality_report_summary)
    return active

def summarize_cost_ledger(ledger: dict[str, Any] | None) -> dict[str, Any]:
    active = dict(ledger or {})
    summary = {
        "task_id": active.get("task_id", ""),
        "domain": active.get("domain", ""),
        "requested_pages": int(active.get("requested_pages", 0) or 0),
        "completed_pages": int(active.get("completed_pages", 0) or 0),
        "stage_records": list(active.get("stage_records") or []),
        "call_records": list(active.get("call_records") or []),
    }
    if active.get("quality_report_summary"):
        summary["quality_report_summary"] = dict(active.get("quality_report_summary") or {})
```

- [ ] **Step 4: Surface fidelity deviations in orchestrated/task summaries**

```python
# agent/workflows/office/orchestrated.py
review={
    "passed": passed,
    "reason": "Office task completed" if passed else "Office task missing validated artifacts",
    "quality_report": quality_report,
    "quality_report_summary": summarize_quality_report(quality_report),
}

# agent/runtime/task_execution.py
detail_lines.extend(quality_report_summary_lines(quality_report))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_cost_logging.py tests/test_office_domain.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/core/quality_report.py agent/workflows/office/orchestrated.py agent/runtime/cost_logging.py agent/runtime/task_execution.py tests/test_cost_logging.py tests/test_office_domain.py
git commit -m "feat: add fidelity diagnostics to office quality summaries"
```

## Task 8: Run Cross-Format Regression Suite

**Files:**
- Modify: `tests/test_office_domain.py`
- Modify: `tests/test_cost_logging.py`
- Modify: `tests/test_shared_officecli.py`

- [ ] **Step 1: Add end-to-end regression tests for create/edit with references**

```python
@pytest.mark.asyncio
async def test_ppt_reference_edit_keeps_goal_first() -> None:
    from agent.workflows.office.reference_resolver import resolve_reference_constraints

    merged = resolve_reference_constraints(
        goal_constraints={"hard_requirements": ["6 slides"]},
        reference_structure_constraints={"units": [{"name": "旧封面"}]},
        reference_style_constraints={"style_tokens": {"theme": "blue"}},
        existing_document_profile={"protected_units": []},
    )

    assert merged["goal_constraints"]["hard_requirements"] == ["6 slides"]


@pytest.mark.asyncio
async def test_xlsx_reference_create_builds_expected_sheet_topology() -> None:
    from agent.workflows.office.strategies.xlsx import XlsxStrategy

    plan = XlsxStrategy().build_plan(
        goal="创建预算分析表",
        requested_slide_count=0,
        build_batch_size=1,
        default_create_file="budget.xlsx",
        merged_constraints={"goal_constraints": {"hard_requirements": ["RawData", "Summary"]}},
    )

    assert [sheet["name"] for sheet in plan["sheets"]] == ["RawData", "Summary"]


@pytest.mark.asyncio
async def test_docx_reference_edit_protects_non_target_sections() -> None:
    from agent.workflows.office.reference_resolver import resolve_reference_constraints

    merged = resolve_reference_constraints(
        goal_constraints={"hard_requirements": ["update risks"]},
        reference_structure_constraints={"units": [{"name": "风险控制"}]},
        reference_style_constraints={"style_tokens": {"heading_style": "Heading1"}},
        existing_document_profile={"protected_units": ["Appendix"]},
    )

    assert merged["existing_document_profile"]["protected_units"] == ["Appendix"]
```

- [ ] **Step 2: Run focused regression suite**

Run: `pytest tests/test_office_domain.py tests/test_cost_logging.py tests/test_shared_officecli.py -q`
Expected: PASS

- [ ] **Step 3: Run full Office-related regression suite**

Run: `pytest tests/test_office_domain.py tests/test_cost_logging.py tests/test_shared_officecli.py tests/test_gateway_desktop_routing.py tests/test_coordinator_phase1.py tests/test_desktop_executor.py tests/test_deepagents_backend.py -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_office_domain.py tests/test_cost_logging.py tests/test_shared_officecli.py
git commit -m "test: add office reference-driven regression coverage"
```

## Spec Coverage Check

- Shared reference layer: Tasks 1-3
- Goal-first conflict resolution: Tasks 1-3
- PPT retrofit: Task 4
- XLSX strategy: Task 5
- DOCX strategy: Task 6
- Fidelity-aware QA and diagnostics: Task 7
- Regression and rollout confidence: Task 8

No spec sections are intentionally omitted from this plan.

## Verification Checklist

- `pptx`, `xlsx`, and `docx` all accept reference files through the same shared model
- `create` and `edit` both use merged constraints rather than ad hoc prompt text
- `goal_constraints` always win over `reference_*_constraints`
- `quality_report_summary` and `cost_ledger` include fidelity-aware diagnostics
- regression suite remains green across existing Office/PPT and desktop runtime tests
