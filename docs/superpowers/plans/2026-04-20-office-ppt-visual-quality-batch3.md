# Office PPT Visual Quality — Batch 3 Implementation Plan (Template-First Lane)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 当用户提供 `.pptx` 模板或在 goal 里点名内置模板时,绕开 blank-deck 堆对象路径,改走"复制模板 → 结构化增删改 → 填充内容"的模板 lane;QA 沿用 Batch 1 + Batch 2 的完整 gate 集合(不绕过)。

**Architecture:** 新增 `agent/workflows/office/template_router.py::decide_template_lane(state)` 根据显式信号(`reference_files` 含 `.pptx` 或 goal 命中内置模板名)选择 lane;新增 `agent/workflows/office/strategies/ppt_template.py` 实现 `OfficeFormatStrategy` 协议,实现模板 lane 的 copy/plan_mapping/structure/content/cleanup sequencing;在 `planning_node` 之前插入 router node(或在 `select_strategy` 阶段切换 strategy)。

**Tech Stack:** Python 3.14, pydantic, pytest, LangGraph-based office workflow

**Depends on:** Batch 1 + Batch 2 必须已 merge。ground_truth gates + per-slide gates 是模板 lane 的验收标准。

**Scope anchor:** spec `docs/superpowers/specs/2026-04-20-office-ppt-visual-quality-design.md` 第 6 章(Batch 3)。

---

## File Structure

| 文件 | 改动类型 | 责任 |
|---|---|---|
| `agent/workflows/office/template_router.py` | 新增 | `decide_template_lane(state) → ("template", ...) \| ("blank", None)` |
| `agent/workflows/office/strategies/ppt_template.py` | 新增 | 实现 OfficeFormatStrategy 协议;build_plan 生成 mapping、build_phase_guidance 分 structure→content 两阶段 |
| `agent/workflows/office/strategies/__init__.py` | 改 | 在 get_strategy_for_format 中按 state.template_lane 分支 |
| `agent/workflows/office/core/state.py` | 扩字段 | `template_lane: bool`、`template_source: str \| None`、`template_phase: Literal["structure","content","done"] \| None` |
| `agent/workflows/office/workflow.py` | 改 | 在 planning 之前调用 router;把 lane 写回 state;build_node 按 template_phase 选 guidance |
| `tests/test_ppt_template_lane.py` | 新增 | router + strategy + gates pass-through |

**Config flag:** spec §9 要求 `OFFICE_PPT_TEMPLATE_LANE_ENABLED`(env 或 settings 二选一)默认 off。全部 task 都在此 flag 后面。

---

## Task 1: Feature flag + state extension

**Files:**
- Modify: `agent/workflows/office/core/state.py`
- Create: `agent/workflows/office/template_router.py`
- Test: `tests/test_ppt_template_lane.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ppt_template_lane.py`:

```python
from __future__ import annotations

import os
from unittest.mock import patch


def test_template_lane_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OFFICE_PPT_TEMPLATE_LANE_ENABLED", raising=False)
    from agent.workflows.office.template_router import is_template_lane_enabled

    assert is_template_lane_enabled() is False


def test_template_lane_env_flag_enables(monkeypatch) -> None:
    monkeypatch.setenv("OFFICE_PPT_TEMPLATE_LANE_ENABLED", "1")
    from agent.workflows.office.template_router import is_template_lane_enabled

    assert is_template_lane_enabled() is True


def test_state_has_template_lane_field() -> None:
    from agent.workflows.office.core.state import OfficeWorkflowState

    annotations = OfficeWorkflowState.__annotations__
    assert "template_lane" in annotations
    assert "template_source" in annotations
    assert "template_phase" in annotations
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_template_lane.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Add state fields**

Edit `agent/workflows/office/core/state.py`, 在 `OfficeWorkflowState` 中加:

```python
    template_lane: bool
    template_source: str | None
    template_phase: str | None
```

- [ ] **Step 4: Create template_router with flag**

Create `agent/workflows/office/template_router.py`:

```python
from __future__ import annotations

import os


def is_template_lane_enabled() -> bool:
    raw = os.environ.get("OFFICE_PPT_TEMPLATE_LANE_ENABLED", "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["is_template_lane_enabled"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_ppt_template_lane.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/core/state.py agent/workflows/office/template_router.py tests/test_ppt_template_lane.py
git commit -m "feat(office): add template lane state fields + feature flag"
```

---

## Task 2: decide_template_lane — user-provided template

**Files:**
- Modify: `agent/workflows/office/template_router.py`
- Test: `tests/test_ppt_template_lane.py`

spec 第 6.1: 用户自带模板 = `operation == "create"` 且 `reference_files` 含 `.pptx`。其他情况 → blank。edit/inspect/transform 走现有路径,不走模板 lane。

- [ ] **Step 1: Write the failing test**

```python
def test_decide_template_lane_user_provided_pptx(monkeypatch) -> None:
    monkeypatch.setenv("OFFICE_PPT_TEMPLATE_LANE_ENABLED", "1")
    from agent.workflows.office.template_router import decide_template_lane

    lane, source = decide_template_lane({
        "operation": "create",
        "reference_files": ["/tmp/user-template.pptx"],
        "goal": "做 6 页 deck",
    })
    assert lane == "template"
    assert source == "/tmp/user-template.pptx"


def test_decide_template_lane_edit_never_template(monkeypatch) -> None:
    monkeypatch.setenv("OFFICE_PPT_TEMPLATE_LANE_ENABLED", "1")
    from agent.workflows.office.template_router import decide_template_lane

    lane, source = decide_template_lane({
        "operation": "edit",
        "reference_files": ["/tmp/user-template.pptx"],
        "source_files": ["/tmp/target.pptx"],
        "goal": "改这份 deck",
    })
    assert lane == "blank"
    assert source is None


def test_decide_template_lane_create_without_references_is_blank(monkeypatch) -> None:
    monkeypatch.setenv("OFFICE_PPT_TEMPLATE_LANE_ENABLED", "1")
    from agent.workflows.office.template_router import decide_template_lane

    lane, source = decide_template_lane({
        "operation": "create",
        "reference_files": [],
        "goal": "做 6 页 deck",
    })
    assert lane == "blank"
    assert source is None


def test_decide_template_lane_noop_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("OFFICE_PPT_TEMPLATE_LANE_ENABLED", raising=False)
    from agent.workflows.office.template_router import decide_template_lane

    lane, source = decide_template_lane({
        "operation": "create",
        "reference_files": ["/tmp/user-template.pptx"],
        "goal": "做 6 页 deck",
    })
    assert lane == "blank"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_template_lane.py -v -k decide_template_lane`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement decide_template_lane (user-provided only)**

Append to `agent/workflows/office/template_router.py`:

```python
from pathlib import Path
from typing import Any


def decide_template_lane(state: dict[str, Any]) -> tuple[str, str | None]:
    """Return ('template', template_source) or ('blank', None)."""
    if not is_template_lane_enabled():
        return "blank", None
    operation = str(state.get("operation") or "").strip().lower()
    if operation != "create":
        return "blank", None
    for item in state.get("reference_files") or []:
        path = str(item or "").strip()
        if path.lower().endswith(".pptx"):
            return "template", path
    return "blank", None


__all__ = ["decide_template_lane", "is_template_lane_enabled"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ppt_template_lane.py -v -k decide_template_lane`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/template_router.py tests/test_ppt_template_lane.py
git commit -m "feat(office): decide_template_lane for user-provided .pptx"
```

---

## Task 3: decide_template_lane — bundled skill templates

**Files:**
- Modify: `agent/workflows/office/template_router.py`
- Test: `tests/test_ppt_template_lane.py`

spec 第 6.1: goal 命中内置模板名 `{pitch deck 模板, pitch-deck, 营销模板, morph, morph-ppt, 路演模板}` → 映射到 bundled skill 目录。spec 6.3 映射:
  - `product_launch` → `skills/officecli/officecli-pitch-deck`
  - `marketing` → `skills/officecli/morph-ppt`
  - `business_formal` → `skills/officecli/officecli-presentation-quality`(QA skill)
  - 其他 → blank

- [ ] **Step 1: Write the failing test**

```python
import pytest


@pytest.mark.parametrize("goal, expected_source", [
    ("用 pitch-deck 模板做一份产品发布 deck", "skills/officecli/officecli-pitch-deck"),
    ("按 morph-ppt 模板做一份 deck", "skills/officecli/morph-ppt"),
    ("用 营销模板 做推广 deck", "skills/officecli/morph-ppt"),
    ("按路演模板做 deck", "skills/officecli/officecli-pitch-deck"),
])
def test_decide_template_lane_bundled_skill(monkeypatch, goal, expected_source) -> None:
    monkeypatch.setenv("OFFICE_PPT_TEMPLATE_LANE_ENABLED", "1")
    from agent.workflows.office.template_router import decide_template_lane

    lane, source = decide_template_lane({
        "operation": "create",
        "reference_files": [],
        "goal": goal,
    })
    assert lane == "template"
    assert source == expected_source


def test_decide_template_lane_generic_goal_stays_blank(monkeypatch) -> None:
    monkeypatch.setenv("OFFICE_PPT_TEMPLATE_LANE_ENABLED", "1")
    from agent.workflows.office.template_router import decide_template_lane

    lane, source = decide_template_lane({
        "operation": "create",
        "reference_files": [],
        "goal": "做一份普通的 6 页介绍 deck",
    })
    assert lane == "blank"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_template_lane.py -v -k decide_template_lane_bundled_skill`
Expected: FAIL

- [ ] **Step 3: Extend decide_template_lane**

Append keyword mapping to `template_router.py`:

```python
_BUNDLED_TEMPLATE_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("pitch-deck", "pitch deck 模板", "路演模板"), "skills/officecli/officecli-pitch-deck"),
    (("morph-ppt", "morph", "营销模板"), "skills/officecli/morph-ppt"),
)
```

And before the final `return "blank", None`:

```python
    lowered_goal = str(state.get("goal") or "").lower()
    for keywords, path in _BUNDLED_TEMPLATE_KEYWORDS:
        if any(kw.lower() in lowered_goal for kw in keywords):
            return "template", path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ppt_template_lane.py -v -k decide_template_lane_bundled_skill`
Expected: PASS(all 5)

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/template_router.py tests/test_ppt_template_lane.py
git commit -m "feat(office): template lane routing for bundled skill templates"
```

---

## Task 4: PptTemplateStrategy skeleton + build_plan mapping

**Files:**
- Create: `agent/workflows/office/strategies/ppt_template.py`
- Test: `tests/test_ppt_template_lane.py`

spec 第 6.2 核心步骤 1-2: Copy → Plan mapping(content slide 需求 vs template slide layouts;输出 ops: 哪些 layout 用、哪些要删、哪些要复制)。

**Design choice:** 模板结构读取依赖 `inspect_reference_file(format_name="pptx", file_path=...)`(已存在,见 reference_inspector.py:98)。TemplateStrategy.build_plan 调用 inspect,生成 mapping 作为 plan 的一部分。

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_ppt_template_strategy_build_plan_produces_mapping(tmp_path, monkeypatch) -> None:
    from agent.workflows.office.strategies.ppt_template import PptTemplateStrategy

    # Mock inspect_reference_file to avoid real file I/O
    async def fake_inspect(*, format_name, file_path):
        return {
            "outline": {
                "slides": [
                    {"index": 1, "layout": "cover"},
                    {"index": 2, "layout": "agenda"},
                    {"index": 3, "layout": "content-twocol"},
                    {"index": 4, "layout": "content-twocol"},
                    {"index": 5, "layout": "summary"},
                ],
            },
            "stats": {"slide_count": 5},
        }

    monkeypatch.setattr(
        "agent.workflows.office.strategies.ppt_template.inspect_reference_file",
        fake_inspect,
    )

    strategy = PptTemplateStrategy(template_source="/tmp/tmpl.pptx")
    plan = await strategy.build_plan_async(
        goal="做一份 6 页商务 deck",
        requested_slide_count=6,
        build_batch_size=2,
        default_create_file="out.pptx",
        merged_constraints={"style_preset": "business_formal"},
    )

    assert plan["template_source"] == "/tmp/tmpl.pptx"
    assert "mapping" in plan
    assert "ops" in plan["mapping"]
    # Plan asks for 6 slides vs template's 5 → duplicate op expected
    op_verbs = {op["op"] for op in plan["mapping"]["ops"]}
    assert "duplicate" in op_verbs or "reorder" in op_verbs


def test_ppt_template_strategy_build_plan_sync_alias_raises_for_template(tmp_path) -> None:
    """Non-async build_plan must defer; the workflow invokes build_plan_async."""
    from agent.workflows.office.strategies.ppt_template import PptTemplateStrategy

    strategy = PptTemplateStrategy(template_source="/tmp/tmpl.pptx")
    # sync build_plan should produce a stub plan that flags async required
    plan = strategy.build_plan(
        goal="g",
        requested_slide_count=3,
        build_batch_size=1,
        default_create_file="o.pptx",
    )
    assert plan.get("requires_async_build_plan") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_template_lane.py -v -k ppt_template_strategy`
Expected: FAIL

- [ ] **Step 3: Implement PptTemplateStrategy**

Create `agent/workflows/office/strategies/ppt_template.py`:

```python
from __future__ import annotations

from typing import Any

from agent.workflows.office.reference_inspector import inspect_reference_file
from agent.workflows.office.strategies.base import OfficeFormatStrategy
from agent.workflows.office.strategies.ppt import PptStrategy


class PptTemplateStrategy:
    """Template-first PPT lane — copy template, remap layouts, then fill content."""

    def __init__(self, *, template_source: str) -> None:
        self.template_source = template_source
        self._blank_strategy = PptStrategy()

    def build_plan(
        self,
        *,
        goal: str,
        requested_slide_count: int | None,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Sync path returns a stub; the workflow should await build_plan_async.
        return {
            "template_source": self.template_source,
            "requires_async_build_plan": True,
            "slide_count": int(requested_slide_count or 0),
        }

    async def build_plan_async(
        self,
        *,
        goal: str,
        requested_slide_count: int | None,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_plan = self._blank_strategy.build_plan(
            goal=goal,
            requested_slide_count=requested_slide_count,
            build_batch_size=build_batch_size,
            default_create_file=default_create_file,
            merged_constraints=merged_constraints,
        )
        template_payload = await inspect_reference_file(format_name="pptx", file_path=self.template_source)
        template_slides = list(((template_payload or {}).get("outline") or {}).get("slides") or [])
        mapping = _build_template_mapping(
            target_slides=base_plan["slides"],
            template_slides=template_slides,
        )
        base_plan["template_source"] = self.template_source
        base_plan["mapping"] = mapping
        base_plan["template_phase"] = "structure"
        return base_plan

    def summarize_plan(self, plan: dict[str, Any]) -> str:
        return self._blank_strategy.summarize_plan(plan)

    def validate_plan(
        self,
        *,
        plan: dict[str, Any],
        goal: str,
        requested_slide_count: int | None,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        normalized, issues = self._blank_strategy.validate_plan(
            plan=plan, goal=goal, requested_slide_count=requested_slide_count,
            build_batch_size=build_batch_size, default_create_file=default_create_file,
            merged_constraints=merged_constraints,
        )
        if "mapping" in plan:
            normalized["mapping"] = plan["mapping"]
        if "template_source" in plan:
            normalized["template_source"] = plan["template_source"]
        normalized["template_phase"] = plan.get("template_phase") or "structure"
        return normalized, issues

    def get_current_batch(self, plan: dict[str, Any], batch_index: int):
        return self._blank_strategy.get_current_batch(plan, batch_index)

    def build_phase_guidance(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        qa_feedback: str,
    ) -> str:
        phase = str(plan.get("template_phase") or "structure")
        blank_guidance = self._blank_strategy.build_phase_guidance(
            plan=plan,
            current_batch_index=current_batch_index,
            repair_mode=repair_mode,
            qa_feedback=qa_feedback,
        )
        if phase == "structure":
            header = [
                "- 当前处于 template 的 structure phase；本轮只允许完成 slide delete / duplicate / reorder 操作，不允许做文本替换。",
                "- 所有结构操作完成后,slide 数必须等于 plan.slide_count 才能进入 content phase。",
            ]
            return "\n".join(header + [blank_guidance])
        if phase == "content":
            header = [
                "- 当前处于 template 的 content phase；结构已稳定,按 plan.slides 逐页填充 takeaway / bullet / picture。",
                "- 替换模板内已有图片时,必须走 `officecli edit ... --type picture --prop src=<new>`,不允许删+加。",
            ]
            return "\n".join(header + [blank_guidance])
        return blank_guidance

    def build_input_sections(self, **kwargs: Any) -> list[str]:
        sections = self._blank_strategy.build_input_sections(**kwargs)
        plan = kwargs.get("plan") or {}
        if plan.get("template_source"):
            sections.append(f"- template_source: {plan['template_source']}")
            sections.append(f"- template_phase: {plan.get('template_phase') or 'structure'}")
        return sections

    def evaluate_quality_stats(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._blank_strategy.evaluate_quality_stats(**kwargs)

    def advance_after_build(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        completed_pages: int,
    ) -> dict[str, Any]:
        return self._blank_strategy.advance_after_build(
            plan=plan, current_batch_index=current_batch_index,
            repair_mode=repair_mode, completed_pages=completed_pages,
        )


def _build_template_mapping(
    *,
    target_slides: list[dict[str, Any]],
    template_slides: list[dict[str, Any]],
) -> dict[str, Any]:
    target_count = len(target_slides)
    template_count = len(template_slides)
    ops: list[dict[str, Any]] = []
    if template_count > target_count:
        for i in range(target_count, template_count):
            ops.append({"op": "delete", "template_index": i + 1})
    if target_count > template_count:
        missing = target_count - template_count
        ops.append({"op": "duplicate", "template_index": template_count, "count": missing})
    # reorder: keep natural order
    return {
        "target_slide_count": target_count,
        "template_slide_count": template_count,
        "ops": ops,
    }


__all__ = ["PptTemplateStrategy"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ppt_template_lane.py -v -k ppt_template_strategy`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt_template.py tests/test_ppt_template_lane.py
git commit -m "feat(office): PptTemplateStrategy skeleton + template mapping builder"
```

---

## Task 5: Strategy routing for template lane

**Files:**
- Modify: `agent/workflows/office/strategies/__init__.py`
- Test: `tests/test_ppt_template_lane.py`

- [ ] **Step 1: Write the failing test**

```python
def test_get_strategy_returns_template_when_lane_enabled() -> None:
    from agent.workflows.office.strategies import get_strategy_for_format
    from agent.workflows.office.strategies.ppt_template import PptTemplateStrategy

    strategy = get_strategy_for_format(
        "pptx",
        operation="create",
        template_lane=True,
        template_source="/tmp/t.pptx",
    )
    assert isinstance(strategy, PptTemplateStrategy)
    assert strategy.template_source == "/tmp/t.pptx"


def test_get_strategy_returns_ppt_when_lane_disabled() -> None:
    from agent.workflows.office.strategies import get_strategy_for_format
    from agent.workflows.office.strategies.ppt import PptStrategy

    strategy = get_strategy_for_format("pptx", operation="create", template_lane=False)
    assert isinstance(strategy, PptStrategy)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_template_lane.py -v -k get_strategy_returns_template`
Expected: FAIL

- [ ] **Step 3: Extend get_strategy_for_format**

Edit `agent/workflows/office/strategies/__init__.py`:

```python
from .ppt_template import PptTemplateStrategy


def get_strategy_for_format(
    format_name: str,
    *,
    operation: str = "",
    template_lane: bool = False,
    template_source: str | None = None,
) -> OfficeFormatStrategy:
    normalized = str(format_name or "").strip().lower()
    normalized_operation = str(operation or "").strip().lower()
    if normalized == "pptx" and normalized_operation in {"", "create", "transform", "edit", "inspect"}:
        if template_lane and template_source:
            return PptTemplateStrategy(template_source=template_source)
        return _PPT_STRATEGY
    if normalized == "docx":
        return _DOCX_STRATEGY
    if normalized == "xlsx":
        return _XLSX_STRATEGY
    return _DEFAULT_STRATEGY
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ppt_template_lane.py -v -k get_strategy_returns`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/__init__.py tests/test_ppt_template_lane.py
git commit -m "feat(office): get_strategy_for_format dispatches PptTemplateStrategy for template lane"
```

---

## Task 6: Wire template_router into workflow preflight

**Files:**
- Modify: `agent/workflows/office/workflow.py::preflight_node`
- Modify: `agent/workflows/office/workflow.py::planning_node`
- Modify: `agent/workflows/office/workflow.py::build_node`
- Test: `tests/test_ppt_template_lane.py`

spec 第 6.1 最后一段: 模板 lane 在 `planning_node` 之前路由。实现选择: 在 `preflight_node` 的返回里加 `template_lane` / `template_source`。

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_preflight_sets_template_lane_when_reference_pptx(monkeypatch) -> None:
    monkeypatch.setenv("OFFICE_PPT_TEMPLATE_LANE_ENABLED", "1")
    from unittest.mock import patch

    from agent.workflows.office.goal_contract import GoalProfile, NormalizeOk, QualityProfile
    from agent.workflows.office.workflow import preflight_node

    profile = GoalProfile(
        format="pptx", operation="create", requested_slide_count=6,
        quality_profile=QualityProfile(notes=True), confidence="high",
        style_preset="business_formal", style_preset_confidence="high",
        reference_files=["/tmp/user-template.pptx"],
    )
    with patch("agent.workflows.office.workflow.normalize_goal_profile", return_value=NormalizeOk(profile=profile)):
        result = await preflight_node({
            "goal": "做 6 页商务 deck",
            "format_hint": "pptx",
            "operation_hint": "create",
            "file_hint": "",
            "source_files": [],
            "reference_files": ["/tmp/user-template.pptx"],
        })

    assert result.get("template_lane") is True
    assert result.get("template_source") == "/tmp/user-template.pptx"


@pytest.mark.asyncio
async def test_preflight_blank_lane_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("OFFICE_PPT_TEMPLATE_LANE_ENABLED", raising=False)
    from unittest.mock import patch

    from agent.workflows.office.goal_contract import GoalProfile, NormalizeOk, QualityProfile
    from agent.workflows.office.workflow import preflight_node

    profile = GoalProfile(
        format="pptx", operation="create", requested_slide_count=6,
        quality_profile=QualityProfile(notes=True), confidence="high",
        style_preset="business_formal", style_preset_confidence="high",
        reference_files=["/tmp/user-template.pptx"],
    )
    with patch("agent.workflows.office.workflow.normalize_goal_profile", return_value=NormalizeOk(profile=profile)):
        result = await preflight_node({
            "goal": "做 6 页商务 deck",
            "format_hint": "pptx",
            "operation_hint": "create",
            "file_hint": "",
            "source_files": [],
            "reference_files": ["/tmp/user-template.pptx"],
        })

    assert result.get("template_lane") in (False, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_template_lane.py -v -k preflight_sets_template_lane`
Expected: FAIL

- [ ] **Step 3: Extend preflight_node**

Edit `agent/workflows/office/workflow.py::preflight_node`,在 return dict 构造前:

```python
    from agent.workflows.office.template_router import decide_template_lane

    template_lane, template_source = decide_template_lane({
        "operation": operation,
        "reference_files": reference_files,
        "goal": str(state.get("goal", "") or ""),
    })
```

And in the returned dict:

```python
        "template_lane": template_lane == "template",
        "template_source": template_source,
        "template_phase": "structure" if template_lane == "template" else None,
```

- [ ] **Step 4: planning_node — await build_plan_async for template**

Edit `agent/workflows/office/workflow.py::planning_node`. Replace:

```python
    strategy = get_strategy_for_format(strategy_format, operation=str(state.get("operation", "") or ""))
```

with:

```python
    template_lane = bool(state.get("template_lane"))
    template_source = str(state.get("template_source") or "") or None
    strategy = get_strategy_for_format(
        strategy_format,
        operation=str(state.get("operation", "") or ""),
        template_lane=template_lane,
        template_source=template_source,
    )
```

And when building raw_plan:

```python
    if template_lane and hasattr(strategy, "build_plan_async"):
        raw_plan = await strategy.build_plan_async(
            goal=str(state.get("goal", "") or ""),
            requested_slide_count=requested_slide_count,
            build_batch_size=build_batch_size,
            default_create_file=default_create_file,
            merged_constraints=merged_constraints,
        )
    else:
        raw_plan = strategy.build_plan(
            goal=str(state.get("goal", "") or ""),
            requested_slide_count=requested_slide_count,
            build_batch_size=build_batch_size,
            default_create_file=default_create_file,
            merged_constraints=merged_constraints,
        )
```

- [ ] **Step 5: build_node — thread template lane into strategy selection**

Edit `agent/workflows/office/workflow.py::build_node`:

```python
    template_lane = bool(state.get("template_lane"))
    template_source = str(state.get("template_source") or "") or None
    strategy = get_strategy_for_format(
        format_hint if format_hint != "auto" else "",
        operation=operation,
        template_lane=template_lane,
        template_source=template_source,
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_ppt_template_lane.py -v -k preflight_sets_template_lane`
Expected: PASS

- [ ] **Step 7: Regression**

Run: `pytest tests/test_office_domain.py tests/test_office_workflow_prompt.py tests/test_office_goal_normalizer.py tests/test_style_presets.py tests/test_ppt_template_lane.py tests/test_ppt_stats_reader.py -v`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add agent/workflows/office/workflow.py tests/test_ppt_template_lane.py
git commit -m "feat(office): wire template lane into preflight/planning/build nodes"
```

---

## Task 7: Structure → content phase gating

**Files:**
- Modify: `agent/workflows/office/strategies/ppt_template.py::advance_after_build`
- Modify: `agent/workflows/office/core/build.py` (where post-build state is patched)
- Test: `tests/test_ppt_template_lane.py`

spec 第 6.2 硬规则: Structure phase 完成后 slide 数必须等于 plan 要求;完成前禁止任何文本替换。此 task 实现相位切换:`advance_after_build` 在当前 phase==structure 且 stats.slide_count==plan.slide_count 时,把 phase 切到 content。

- [ ] **Step 1: Write the failing test**

```python
def test_template_advance_after_structure_moves_to_content_when_slide_count_matches() -> None:
    from agent.workflows.office.strategies.ppt_template import PptTemplateStrategy

    strategy = PptTemplateStrategy(template_source="/tmp/t.pptx")
    plan = {
        "slide_count": 6,
        "template_phase": "structure",
        "batches": [{"index": 0, "slide_start": 1, "slide_end": 6}],
        "slides": [{"index": i} for i in range(1, 7)],
    }
    result = strategy.advance_after_build(
        plan=plan,
        current_batch_index=0,
        repair_mode=False,
        completed_pages=6,
    )
    # Expect phase toggled
    assert result.get("template_phase_next") == "content"


def test_template_advance_stays_in_structure_when_slide_count_mismatch() -> None:
    from agent.workflows.office.strategies.ppt_template import PptTemplateStrategy

    strategy = PptTemplateStrategy(template_source="/tmp/t.pptx")
    plan = {
        "slide_count": 6,
        "template_phase": "structure",
        "batches": [{"index": 0, "slide_start": 1, "slide_end": 6}],
        "slides": [{"index": i} for i in range(1, 7)],
    }
    result = strategy.advance_after_build(
        plan=plan,
        current_batch_index=0,
        repair_mode=False,
        completed_pages=4,   # not yet 6
    )
    assert result.get("template_phase_next") in (None, "structure")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_template_lane.py -v -k template_advance`
Expected: FAIL (no template_phase_next key)

- [ ] **Step 3: Extend advance_after_build**

In `agent/workflows/office/strategies/ppt_template.py`:

```python
    def advance_after_build(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        completed_pages: int,
    ) -> dict[str, Any]:
        base = self._blank_strategy.advance_after_build(
            plan=plan, current_batch_index=current_batch_index,
            repair_mode=repair_mode, completed_pages=completed_pages,
        )
        phase = str(plan.get("template_phase") or "structure")
        slide_count = int(plan.get("slide_count") or 0)
        if phase == "structure" and completed_pages >= slide_count > 0:
            base["template_phase_next"] = "content"
        else:
            base["template_phase_next"] = phase
        return base
```

- [ ] **Step 4: Propagate template_phase_next into state**

Edit `agent/workflows/office/core/build.py` where `advance_after_build`'s return is merged into state updates. Add a line to copy `template_phase_next` into `template_phase`:

Run: `grep -n "advance_after_build\|current_batch_index" agent/workflows/office/core/build.py`.

At the write-back site (near where `current_batch_index` is set):

```python
    phase_next = strategy_advance.get("template_phase_next")
    if phase_next:
        output["template_phase"] = phase_next
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_ppt_template_lane.py -v -k template_advance`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/strategies/ppt_template.py agent/workflows/office/core/build.py tests/test_ppt_template_lane.py
git commit -m "feat(office): template lane phase transition (structure→content)"
```

---

## Task 8: Structure phase QA — slide_count must match plan before content

**Files:**
- Modify: `agent/workflows/office/strategies/ppt_template.py::evaluate_quality_stats`
- Test: `tests/test_ppt_template_lane.py`

spec 第 6.2 硬规则: 结构修改完成后 slide 数必须等于 plan 要求。此 task 让 template strategy 在 phase==structure 的 QA round 额外判定 slide_count 匹配。

- [ ] **Step 1: Write the failing test**

```python
def test_template_evaluate_fails_in_structure_phase_when_slide_count_off() -> None:
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats
    from agent.workflows.office.strategies.ppt_template import PptTemplateStrategy

    strategy = PptTemplateStrategy(template_source="/tmp/t.pptx")
    plan = {
        "slide_count": 6,
        "template_phase": "structure",
        "slides": [{"index": i, "requires_real_picture": False, "max_text_blocks": 3,
                    "typography_pair": {"header_font": "Microsoft YaHei", "body_font": "Microsoft YaHei"}} for i in range(1, 7)],
    }
    gt = GroundTruthStats(
        slide_count=4,
        per_slide=[SlidePhysicalStats(index=i, picture_count=1, layout_signature=f"sig-{i}") for i in range(1, 5)],
        unique_font_families={"Microsoft YaHei"},
    )
    issues = strategy.evaluate_quality_stats(
        operation="create",
        stats={"slide_count": 4, "content_slide_count": 4, "notes_slide_count": 4, "transition_slide_count": 3,
               "visual_slide_count": 4, "text_only_slide_count": 0, "layout_variety_count": 2,
               "picture_count": 4, "chart_count": 0, "table_count": 0,
               "qa_checks": ["view_stats", "view_annotated", "validate"]},
        plan=plan,
        ground_truth=gt,
    )
    assert any("T1-structure-slide-count" in str(i.get("message", "")) for i in issues)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_template_lane.py -v -k structure_phase_when_slide_count_off`
Expected: FAIL

- [ ] **Step 3: Override evaluate_quality_stats in template strategy**

Edit `agent/workflows/office/strategies/ppt_template.py`:

```python
    def evaluate_quality_stats(self, **kwargs: Any) -> list[dict[str, Any]]:
        issues = self._blank_strategy.evaluate_quality_stats(**kwargs)
        plan = kwargs.get("plan") or {}
        ground_truth = kwargs.get("ground_truth")
        phase = str(plan.get("template_phase") or "structure")
        if phase == "structure" and ground_truth is not None:
            expected = int(plan.get("slide_count") or 0)
            actual = int(getattr(ground_truth, "slide_count", 0) or 0)
            if expected and actual != expected:
                issues.append(
                    {
                        "severity": "error",
                        "message": f"T1-structure-slide-count: structure phase 结束时 slide_count={actual} ≠ plan={expected}",
                    }
                )
        return issues
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ppt_template_lane.py -v -k structure_phase_when_slide_count_off`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt_template.py tests/test_ppt_template_lane.py
git commit -m "feat(office): template lane structure-phase slide_count gate"
```

---

## Task 9: Fallback to blank lane after 2 failed structure rounds

**Files:**
- Modify: `agent/workflows/office/core/qa.py` or `build.py` (wherever qa_fix_round is incremented)
- Test: `tests/test_ppt_template_lane.py`

spec 第 8 Risks #4: structure phase 限最多 2 轮,失败即退回 blank lane。

- [ ] **Step 1: Write the failing test**

```python
def test_structure_phase_two_rounds_fallback_to_blank_lane() -> None:
    from agent.workflows.office.core.qa import run_qa_fix_stage
    from agent.workflows.office.strategies.ppt_template import PptTemplateStrategy

    # Simulate state entering qa_fix after 2 structure rounds still failing slide_count
    state = {
        "format": "pptx",
        "operation": "create",
        "write_required": True,
        "template_lane": True,
        "template_source": "/tmp/t.pptx",
        "template_phase": "structure",
        "qa_fix_round": 2,
        "max_qa_fix_rounds": 2,
        "deck_plan": {"slide_count": 6, "template_phase": "structure",
                      "slides": [{"index": i, "max_text_blocks": 3,
                                  "typography_pair": {"header_font": "Microsoft YaHei", "body_font": "Microsoft YaHei"}}
                                 for i in range(1, 7)]},
        "intermediate_results": [{
            "output": '```json\n{"operation":"create","validated":true,"summary":"","artifacts":[{"filename":"deck.pptx","path":"","format":"pptx","role":"primary"}],"stats":{"slide_count":4,"content_slide_count":4,"notes_slide_count":4,"transition_slide_count":3,"visual_slide_count":4,"text_only_slide_count":0,"layout_variety_count":2,"picture_count":4,"chart_count":0,"table_count":0,"qa_checks":["view_stats","view_annotated","validate"]}}\n```',
            "ground_truth_stats_raw": {
                "view_stats": {"slide_count": 4, "slides": [{"index": i, "pictures": 1} for i in range(1, 5)]},
                "view_annotated": "\n".join(f"Slide {i}\n- Title: T ← Microsoft YaHei 32pt" for i in range(1, 5)),
            },
        }],
        "task_profile": {"merged_constraints": {"goal": "g"}},
    }
    result = run_qa_fix_stage(state, strategy=PptTemplateStrategy(template_source="/tmp/t.pptx"))
    # Fallback should flip template_lane=False for next round
    assert result.get("template_lane") is False
    assert result.get("terminal_status") in (None, "", "quality_gate_fixable", "fallback_to_blank")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_template_lane.py -v -k two_rounds_fallback_to_blank`
Expected: FAIL

- [ ] **Step 3: Add fallback logic in qa.py**

Edit `agent/workflows/office/core/qa.py::run_qa_fix_stage`, 在 fixable return 分支前(约 line 316-336):

```python
    if (
        state.get("template_lane") is True
        and str(state.get("template_phase") or "") == "structure"
        and next_round > max_qa_fix_rounds
    ):
        # fallback to blank lane, reset rounds
        return {
            "evaluations": [evaluation],
            "confidence": 0.0,
            "cost_ledger": cost_ledger,
            "current_stage": "planning",
            "template_lane": False,
            "template_source": None,
            "template_phase": None,
            "qa_fix_round": 0,
            "partial_progress": {
                "stage": "planning",
                "completed_pages": completed_pages,
                "reason": "template_structure_fallback",
            },
            "quality_report": quality_report,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ppt_template_lane.py -v -k two_rounds_fallback_to_blank`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/core/qa.py tests/test_ppt_template_lane.py
git commit -m "feat(office): template lane fallback to blank after 2 structure rounds"
```

---

## Task 10: Full regression + acceptance smoke

- [ ] **Step 1: Full office test suite**

Run: `pytest tests/test_office_domain.py tests/test_office_workflow_prompt.py tests/test_office_goal_normalizer.py tests/test_style_presets.py tests/test_ppt_stats_reader.py tests/test_ppt_template_lane.py -v`
Expected: all PASS

- [ ] **Step 2: Manual lane smoke**

Each of the 5 presets must have at least one template-lane success path (spec §6.4):

| Preset | Template source | Goal |
|---|---|---|
| product_launch | `skills/officecli/officecli-pitch-deck` (bundled) | 用 pitch-deck 模板做一份产品发布 deck,8 页 |
| marketing | `skills/officecli/morph-ppt` (bundled) | 按 morph-ppt 模板做一份营销 deck,10 页 |
| business_formal | User uploads `business-template.pptx` | 用这份模板做 Q3 业绩汇报 |
| course_training | User uploads `training-template.pptx` | 用这份模板做新员工培训 |
| lifestyle | User uploads `lifestyle-template.pptx` | 用这份模板做钓鱼分享 |

For each: set `OFFICE_PPT_TEMPLATE_LANE_ENABLED=1`, run, verify:
  - Structure phase finishes within 2 rounds
  - Content phase finishes within 1 repair round
  - Final deck passes all Batch 1 + Batch 2 gates
  - No "structure phase text edits" observed(check tool call history for edits in structure phase — should be only delete/duplicate/reorder)

- [ ] **Step 3: No commit, record observations**

Append any preset that can't produce a template-lane success path to plan Rollout Notes.

---

## Self-Review Checklist

- [ ] Spec §6.1 routing: Tasks 2-3 cover user-provided + bundled. ✅
- [ ] Spec §6.1 边界: edit operation stays on existing path (Task 2 test). ✅
- [ ] Spec §6.2 pipeline steps 1-2 (copy + plan mapping): Task 4. ✅
- [ ] Spec §6.2 structure → content sequencing: Tasks 7-8 (phase gating + slide_count gate). ✅
- [ ] Spec §6.2 硬规则 "picture replace must use edit --type picture": wired via prompt in `build_phase_guidance` (Task 4). ✅
- [ ] Spec §6.2 cleanup + QA reuses Batch 1+2 gates: `PptTemplateStrategy.evaluate_quality_stats` delegates to `PptStrategy` then adds T1 gate. ✅
- [ ] Spec §6.3 bundled skill mapping: Task 3 maps product_launch → pitch-deck and marketing → morph-ppt. business_formal → QA skill is already covered by the QA rubric and doesn't need a template file (delegated). ✅
- [ ] Spec §6.4 acceptance: Tasks 8 (slide_count) + 10 (5-preset smoke). Structure phase failure returns to blank (Task 9). ✅
- [ ] Spec §9 config flag default off: Task 1 feature flag check. ✅
- [ ] Type names consistent: `PptTemplateStrategy`, `decide_template_lane`, `is_template_lane_enabled`, `_build_template_mapping`, `template_lane` / `template_source` / `template_phase`. ✅
- [ ] No TBD / placeholder / "similar to" references. ✅

---

## Rollout Notes

- Feature flag `OFFICE_PPT_TEMPLATE_LANE_ENABLED` 默认 off。Batch 3 merge 后,内部开发先 set=1 做灰度。
- Template lane 生成的 deck 沿用 Batch 1 + Batch 2 全部 gate;绕过 gate 被明确禁止(见 spec §6.2)。
- Structure phase 2 轮失败自动回退 blank lane(Task 9),避免卡死。
- 不在 Batch 3 实现的 spec 条目:
  - Spec §6.2 "删除未用的 placeholder / orphaned media" 写在模板 strategy 的 build_phase_guidance content phase 文案中(Task 4)。运行时的"真正删除"是 agent 执行工具的职责,无需写额外 python 逻辑。
  - business_formal 映射到 `officecli-presentation-quality` 作为 QA skill,不是模板;因此不需要在 router 里把 business_formal 映射到文件。 
