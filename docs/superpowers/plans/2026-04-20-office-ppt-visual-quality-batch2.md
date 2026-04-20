# Office PPT Visual Quality — Batch 2 Implementation Plan (Per-Slide Taxonomy + Style Preset + Vertical 澄清)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Batch 1 真值化 stats 的基础上,给每页加上 page_type / content_subtype / requires_real_picture / typography_pair / theme_ref 等 taxonomy 字段,配套 5 个 style preset 做视觉合同,并在 goal normalizer 中推断 vertical;低置信度触发 interrupt 澄清。

**Architecture:** 新增 `agent/workflows/office/style_presets.py` 定义 5 个 preset;`GoalProfile` 扩 `style_preset` + `style_preset_confidence`;`goal_normalizer.py` 在 LLM extract 后加 vertical inference;`strategies/ppt.py::build_plan` / `validate_plan` 扩 slide schema 生成 taxonomy 字段;`evaluate_quality_stats` 再加 per-slide pass(requires_real_picture / max_text_blocks / font_family subset)。保持现有 LangGraph 拓扑。

**Tech Stack:** Python 3.14, pydantic, pytest, LangGraph-based office workflow

**Depends on:** Batch 1 必须已 merge(本 plan 中假定 `GroundTruthStats` / ground_truth gate 已可用)。

**Scope anchor:** spec `docs/superpowers/specs/2026-04-20-office-ppt-visual-quality-design.md` 第 5 章(Batch 2)。

---

## File Structure

| 文件 | 改动类型 | 责任 |
|---|---|---|
| `agent/workflows/office/style_presets.py` | 新增 | 5 个 StylePreset 的常量数据结构 |
| `agent/workflows/office/goal_contract.py` | 扩字段 | `GoalProfile.style_preset` + `style_preset_confidence` |
| `agent/workflows/office/goal_normalizer.py` | 扩 prompt + vertical inference | 新增 `infer_style_preset` / `_extract_style_preset` |
| `agent/workflows/office/strategies/ppt.py` | 扩 slide schema + per-slide gate | 每页加 taxonomy 字段;evaluate_quality_stats 加 per-slide pass |
| `agent/workflows/office/core/state.py` | 扩字段 | `style_preset: str \| None` |
| `agent/workflows/office/workflow.py::preflight_node` | 改 | confidence=low 时 interrupt 澄清 vertical |
| `tests/test_style_presets.py` | 新增 | preset 定义基本断言 |
| `tests/test_office_goal_normalizer.py` | 增 case | vertical inference |
| `tests/test_office_domain.py` | 增 case | per-slide gate、preflight 澄清 |

---

## Task 1: StylePreset 数据结构 + 5 个 preset 定义

**Files:**
- Create: `agent/workflows/office/style_presets.py`
- Test: `tests/test_style_presets.py`

spec 第 5.3: 5 个 preset(business_formal / marketing / product_launch / course_training / lifestyle),每个含 theme (primary/secondary/accent/light/bg)、typography (header/body)、corner_radius、layout_rotation、hero_style。

- [ ] **Step 1: Write the failing test**

Create `tests/test_style_presets.py`:

```python
from __future__ import annotations

import pytest


def test_all_five_presets_exist() -> None:
    from agent.workflows.office.style_presets import STYLE_PRESETS

    assert set(STYLE_PRESETS.keys()) == {
        "business_formal",
        "marketing",
        "product_launch",
        "course_training",
        "lifestyle",
    }


@pytest.mark.parametrize("name", [
    "business_formal", "marketing", "product_launch", "course_training", "lifestyle",
])
def test_preset_has_five_theme_colors_and_typography_pair(name: str) -> None:
    from agent.workflows.office.style_presets import STYLE_PRESETS

    preset = STYLE_PRESETS[name]
    assert set(preset.theme.keys()) == {"primary", "secondary", "accent", "light", "bg"}
    assert preset.typography.header
    assert preset.typography.body
    assert len(preset.layout_rotation) >= 3


def test_preset_typography_uses_allowed_fonts() -> None:
    from agent.workflows.office.style_presets import STYLE_PRESETS

    allowed = {"Microsoft YaHei", "Arial"}
    for preset in STYLE_PRESETS.values():
        assert preset.typography.header in allowed
        assert preset.typography.body in allowed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_style_presets.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Create style_presets.py**

Create `agent/workflows/office/style_presets.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class TypographyPair:
    header: str
    body: str


@dataclass(frozen=True)
class StylePreset:
    name: str
    theme: dict[str, str]
    typography: TypographyPair
    corner_radius: float
    layout_rotation: tuple[str, ...]
    hero_style: str


StylePresetName = Literal[
    "business_formal",
    "marketing",
    "product_launch",
    "course_training",
    "lifestyle",
]


_YAHEI_PAIR = TypographyPair(header="Microsoft YaHei", body="Microsoft YaHei")


STYLE_PRESETS: dict[str, StylePreset] = {
    "business_formal": StylePreset(
        name="business_formal",
        theme={"primary": "1F3864", "secondary": "2E75B6", "accent": "C55A11", "light": "F2F2F2", "bg": "FFFFFF"},
        typography=_YAHEI_PAIR,
        corner_radius=0.05,
        layout_rotation=("two-column", "cards-grid", "big-number", "timeline"),
        hero_style="shape_divider",
    ),
    "marketing": StylePreset(
        name="marketing",
        theme={"primary": "A4262C", "secondary": "E8A33D", "accent": "54408C", "light": "FFF4E6", "bg": "FFFFFF"},
        typography=_YAHEI_PAIR,
        corner_radius=0.15,
        layout_rotation=("big-number", "cards-grid", "comparison", "timeline"),
        hero_style="full_bleed_picture",
    ),
    "product_launch": StylePreset(
        name="product_launch",
        theme={"primary": "111111", "secondary": "3D5AFE", "accent": "00E5FF", "light": "F4F6FB", "bg": "FFFFFF"},
        typography=_YAHEI_PAIR,
        corner_radius=0.25,
        layout_rotation=("hero-pill", "big-number", "feature-cards", "timeline"),
        hero_style="pill_hero",
    ),
    "course_training": StylePreset(
        name="course_training",
        theme={"primary": "1B6F6F", "secondary": "5FA8A8", "accent": "E0B341", "light": "EDF5F5", "bg": "FFFFFF"},
        typography=_YAHEI_PAIR,
        corner_radius=0.10,
        layout_rotation=("two-column", "process-flow", "cards-grid", "timeline"),
        hero_style="soft_divider",
    ),
    "lifestyle": StylePreset(
        name="lifestyle",
        theme={"primary": "2E4A3F", "secondary": "7FA48A", "accent": "E5A35B", "light": "F7F2EA", "bg": "FFFFFF"},
        typography=_YAHEI_PAIR,
        corner_radius=0.20,
        layout_rotation=("full-bleed-image", "image-showcase", "cards-grid", "timeline"),
        hero_style="full_bleed_picture",
    ),
}


def get_preset(name: str | None) -> StylePreset:
    key = str(name or "").strip().lower()
    return STYLE_PRESETS.get(key, STYLE_PRESETS["business_formal"])


__all__ = ["STYLE_PRESETS", "StylePreset", "StylePresetName", "TypographyPair", "get_preset"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_style_presets.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/style_presets.py tests/test_style_presets.py
git commit -m "feat(office): add 5 style presets (business_formal/marketing/product_launch/course_training/lifestyle)"
```

---

## Task 2: GoalProfile 扩 style_preset 字段

**Files:**
- Modify: `agent/workflows/office/goal_contract.py:30-39`
- Test: `tests/test_office_goal_normalizer.py`

spec 第 5.4: `GoalProfile` 加 `style_preset` 与 `style_preset_confidence`(复用 `GoalConfidence` literal)。

- [ ] **Step 1: Write the failing test**

Append to `tests/test_office_goal_normalizer.py`:

```python
def test_goal_profile_has_style_preset_fields() -> None:
    from agent.workflows.office.goal_contract import GoalProfile

    profile = GoalProfile()
    assert hasattr(profile, "style_preset")
    assert hasattr(profile, "style_preset_confidence")
    assert profile.style_preset is None
    assert profile.style_preset_confidence == "low"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_goal_normalizer.py -v -k goal_profile_has_style_preset_fields`
Expected: FAIL

- [ ] **Step 3: Extend GoalProfile**

Edit `agent/workflows/office/goal_contract.py` — top of file:

```python
StylePresetName: TypeAlias = Literal[
    "business_formal",
    "marketing",
    "product_launch",
    "course_training",
    "lifestyle",
]
```

Extend `GoalProfile` (line 30):

```python
class GoalProfile(BaseModel):
    format: OfficeFormat | None = None
    operation: OfficeOperation | None = None
    requested_slide_count: int | None = Field(default=None, ge=1, le=30)
    output_filename: str | None = None
    source_files: list[str] = Field(default_factory=list)
    reference_files: list[str] = Field(default_factory=list)
    quality_profile: QualityProfile = Field(default_factory=QualityProfile)
    confidence: GoalConfidence = "low"
    missing_fields: list[str] = Field(default_factory=list)
    style_preset: StylePresetName | None = None
    style_preset_confidence: GoalConfidence = "low"
```

Add `StylePresetName` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_goal_normalizer.py -v -k goal_profile_has_style_preset_fields`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/goal_contract.py tests/test_office_goal_normalizer.py
git commit -m "feat(office): add style_preset fields to GoalProfile"
```

---

## Task 3: Keyword-based vertical inference

**Files:**
- Modify: `agent/workflows/office/goal_normalizer.py`
- Test: `tests/test_office_goal_normalizer.py`

spec 第 5.4 关键词辅助表。此 task 只做 keyword-based 推断作为 fallback;Task 4 再接 LLM extract。

- [ ] **Step 1: Write the failing test**

```python
import pytest


@pytest.mark.parametrize("goal, expected_preset, expected_confidence", [
    ("做一份 Q4 季度汇报 PPT 给董事会", "business_formal", "high"),
    ("营销推广活动方案，campaign deck", "marketing", "high"),
    ("产品发布会 launch deck", "product_launch", "high"),
    ("新员工培训课程 workshop", "course_training", "high"),
    ("钓鱼好处的生活分享", "lifestyle", "high"),
    ("今天写点东西", None, "low"),  # no keyword hits -> low
])
def test_infer_style_preset_keyword_rules(goal, expected_preset, expected_confidence) -> None:
    from agent.workflows.office.goal_normalizer import infer_style_preset

    preset, confidence = infer_style_preset(goal)
    assert preset == expected_preset
    assert confidence == expected_confidence


def test_infer_style_preset_multiple_keywords_drops_to_low() -> None:
    from agent.workflows.office.goal_normalizer import infer_style_preset

    preset, confidence = infer_style_preset("既是商务报告又是产品发布的培训课程")
    assert confidence == "low"
    assert preset is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_goal_normalizer.py -v -k infer_style_preset`
Expected: FAIL

- [ ] **Step 3: Implement infer_style_preset**

Edit `agent/workflows/office/goal_normalizer.py`,在文件顶部加:

```python
_STYLE_PRESET_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("business_formal", ("商务", "汇报", "季度", "q1", "q2", "q3", "q4", "董事会", "board")),
    ("marketing", ("营销", "推广", "campaign", "发布会")),
    ("product_launch", ("产品", "发布", "launch", "上线")),
    ("course_training", ("培训", "教程", "课程", "workshop", "教学")),
    ("lifestyle", ("生活", "旅行", "美食", "钓鱼", "健身", "lifestyle")),
)


def infer_style_preset(goal: str) -> tuple[str | None, str]:
    """Keyword-based vertical inference. Returns (preset_name_or_None, confidence)."""
    lowered = str(goal or "").lower()
    hits: list[str] = []
    for name, keywords in _STYLE_PRESET_KEYWORDS:
        if any(kw.lower() in lowered for kw in keywords):
            hits.append(name)
    if len(hits) == 1:
        return hits[0], "high"
    return None, "low"
```

Note: "marketing" vs "product_launch" 可能同时命中(如 "营销发布会")。按 spec:"命中多个或全无 → low"。因此多命中=low。

Add `infer_style_preset` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_goal_normalizer.py -v -k infer_style_preset`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/goal_normalizer.py tests/test_office_goal_normalizer.py
git commit -m "feat(office): keyword-based style_preset inference"
```

---

## Task 4: Wire infer_style_preset into normalize_goal_profile

**Files:**
- Modify: `agent/workflows/office/goal_normalizer.py::normalize_goal_profile`
- Test: `tests/test_office_goal_normalizer.py`

spec 第 5.4: normalize 最终给 profile 填 `style_preset` + `style_preset_confidence`。此 task 只接 keyword inference;LLM extract 的 style_preset 留做 future optimization(YAGNI)。

- [ ] **Step 1: Write the failing test**

```python
import pytest


@pytest.mark.asyncio
async def test_normalize_goal_profile_fills_style_preset_for_business_formal() -> None:
    from agent.workflows.office.goal_contract import GoalNormalizationRequest, NormalizeOk
    from agent.workflows.office.goal_normalizer import normalize_goal_profile

    req = GoalNormalizationRequest(
        raw_user_message="给董事会做一份 Q3 业绩汇报 PPT，8 页",
        explicit_format="pptx",
        explicit_operation="create",
    )
    result = await normalize_goal_profile(req)
    assert isinstance(result, NormalizeOk)
    assert result.profile.style_preset == "business_formal"
    assert result.profile.style_preset_confidence == "high"


@pytest.mark.asyncio
async def test_normalize_goal_profile_low_confidence_when_no_keyword() -> None:
    from agent.workflows.office.goal_contract import GoalNormalizationRequest, NormalizeOk
    from agent.workflows.office.goal_normalizer import normalize_goal_profile

    req = GoalNormalizationRequest(
        raw_user_message="做一份 6 页 PPT",
        explicit_format="pptx",
        explicit_operation="create",
    )
    result = await normalize_goal_profile(req)
    assert isinstance(result, NormalizeOk)
    assert result.profile.style_preset is None
    assert result.profile.style_preset_confidence == "low"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_goal_normalizer.py -v -k style_preset -m asyncio`
Expected: FAIL

- [ ] **Step 3: Wire into normalize_goal_profile**

Edit `agent/workflows/office/goal_normalizer.py`, 在 `profile = GoalProfile(...)` 构造块 (around line 466-480) 插入 style preset 字段:

```python
    style_preset, style_preset_confidence = infer_style_preset(raw_user_message)

    profile = GoalProfile(
        format=format_name,
        operation=operation,
        requested_slide_count=requested_slide_count,
        output_filename=output_filename,
        source_files=normalized_source_files,
        reference_files=normalized_reference_files,
        quality_profile=merged_quality,
        confidence=_resolve_confidence(
            extracted_confidence=extracted.confidence,
            raw_slide_count=raw_slide_count,
            extracted_slide_count=_normalize_slide_count(extracted.requested_slide_count),
        ),
        missing_fields=[],
        style_preset=style_preset,
        style_preset_confidence=style_preset_confidence,
    )
    return NormalizeOk(profile=profile)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_goal_normalizer.py -v -k style_preset`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/goal_normalizer.py tests/test_office_goal_normalizer.py
git commit -m "feat(office): populate style_preset in normalize_goal_profile"
```

---

## Task 5: preflight_node 低置信度触发 interrupt

**Files:**
- Modify: `agent/workflows/office/workflow.py::preflight_node`
- Test: `tests/test_office_domain.py`

spec 第 5.4 (B 方案): `style_preset_confidence == "low"` → 通过 `request_interrupt` 发澄清;placeholder = "例如：商务汇报 / 营销推广 / 产品发布 / 培训课程 / 生活方式"。

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_preflight_triggers_interrupt_when_style_preset_low() -> None:
    from unittest.mock import patch

    from agent.workflows.office.goal_contract import GoalProfile, NormalizeOk, QualityProfile
    from agent.workflows.office.workflow import preflight_node

    profile = GoalProfile(
        format="pptx",
        operation="create",
        requested_slide_count=6,
        quality_profile=QualityProfile(visuals=False, animations=False, notes=True),
        confidence="high",
        style_preset=None,
        style_preset_confidence="low",
    )
    captured = {}

    def fake_request_interrupt(payload, *args, **kwargs):
        captured["payload"] = payload
        return "商务汇报"  # user answer

    with patch("agent.workflows.office.workflow.normalize_goal_profile", return_value=NormalizeOk(profile=profile)), \
         patch("agent.workflows.office.workflow.request_interrupt", side_effect=fake_request_interrupt):
        result = await preflight_node({
            "goal": "做一份 6 页 PPT",
            "format_hint": "pptx",
            "operation_hint": "create",
            "file_hint": "",
            "source_files": [],
        })

    assert "payload" in captured
    assert "商务汇报" in captured["payload"].get("placeholder", "")
    assert result.get("task_profile", {}).get("style_preset") == "business_formal"


@pytest.mark.asyncio
async def test_preflight_skips_interrupt_when_style_preset_high() -> None:
    from unittest.mock import patch

    from agent.workflows.office.goal_contract import GoalProfile, NormalizeOk, QualityProfile
    from agent.workflows.office.workflow import preflight_node

    profile = GoalProfile(
        format="pptx", operation="create", requested_slide_count=6,
        quality_profile=QualityProfile(notes=True), confidence="high",
        style_preset="marketing", style_preset_confidence="high",
    )
    called = {"count": 0}

    def fake_request_interrupt(*args, **kwargs):
        called["count"] += 1
        return ""

    with patch("agent.workflows.office.workflow.normalize_goal_profile", return_value=NormalizeOk(profile=profile)), \
         patch("agent.workflows.office.workflow.request_interrupt", side_effect=fake_request_interrupt):
        result = await preflight_node({
            "goal": "营销活动推广 deck",
            "format_hint": "pptx",
            "operation_hint": "create",
            "file_hint": "",
            "source_files": [],
        })

    assert called["count"] == 0
    assert result.get("task_profile", {}).get("style_preset") == "marketing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k preflight_triggers_interrupt_when_style_preset_low`
Expected: FAIL

- [ ] **Step 3: Extend preflight_node**

Edit `agent/workflows/office/workflow.py::preflight_node`(around line 399-473). After `profile = normalized.profile` but before constructing the return dict:

```python
    style_preset = profile.style_preset
    style_preset_confidence = profile.style_preset_confidence
    if style_preset_confidence == "low":
        answer = request_interrupt(
            {
                "content": "这份 PPT 的视觉风格倾向哪类?",
                "context": "帮我判断应该走 商务汇报 / 营销推广 / 产品发布 / 培训课程 / 生活方式 中的哪一套视觉系统。",
                "placeholder": "例如：商务汇报 / 营销推广 / 产品发布 / 培训课程 / 生活方式",
                "interrupt_type": "clarification",
            }
        )
        from agent.workflows.office.goal_normalizer import infer_style_preset

        inferred, confidence = infer_style_preset(str(answer or ""))
        if inferred is None:
            inferred = "business_formal"
            confidence = "low"
        style_preset = inferred
        style_preset_confidence = confidence
```

Add `style_preset` and `style_preset_confidence` to the returned `task_profile` and top-level state:

```python
    return {
        # ...existing fields...
        "style_preset": style_preset,
        "task_profile": {
            # ...existing subkeys...
            "style_preset": style_preset,
            "style_preset_confidence": style_preset_confidence,
        },
        # ...
    }
```

- [ ] **Step 4: Add `style_preset` to state schema**

Edit `agent/workflows/office/core/state.py`, 在 `OfficeWorkflowState` 中加:

```python
    style_preset: str | None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k preflight_triggers_interrupt_when_style_preset`
Expected: PASS (both cases)

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/workflow.py agent/workflows/office/core/state.py tests/test_office_domain.py
git commit -m "feat(office): clarify style_preset via interrupt when confidence=low"
```

---

## Task 6: PPT slide schema 扩 taxonomy 字段

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py:19-32` (build_plan._build_slide) + :120-148 (validate_plan)
- Test: `tests/test_office_domain.py`

spec 第 5.1: 每 slide 新增 `page_type / content_subtype / requires_real_picture / max_text_blocks / typography_pair / theme_ref`。`requires_real_picture` 默认规则: cover → True, content_subtype ∈ {image_showcase, timeline, comparison} → True。

- [ ] **Step 1: Write the failing test**

```python
def test_ppt_build_plan_adds_taxonomy_fields() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy

    plan = PptStrategy().build_plan(
        goal="做一份 6 页 PPT",
        requested_slide_count=6,
        build_batch_size=2,
        default_create_file="deck.pptx",
        merged_constraints={"style_preset": "business_formal"},
    )
    slides = plan["slides"]
    assert len(slides) == 6
    for s in slides:
        assert "page_type" in s
        assert "content_subtype" in s
        assert "requires_real_picture" in s
        assert "max_text_blocks" in s
        assert "typography_pair" in s
        assert "theme_ref" in s
    # cover slide requires picture
    assert slides[0]["page_type"] == "cover"
    assert slides[0]["requires_real_picture"] is True
    # typography uses YaHei
    assert slides[0]["typography_pair"]["header_font"] == "Microsoft YaHei"


def test_ppt_build_plan_defaults_to_business_formal_when_missing_preset() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy

    plan = PptStrategy().build_plan(
        goal="做一份 6 页 PPT",
        requested_slide_count=6,
        build_batch_size=2,
        default_create_file="deck.pptx",
    )
    slides = plan["slides"]
    assert slides[0]["theme_ref"] == "business_formal:primary"


def test_ppt_build_plan_image_showcase_requires_picture() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy

    plan = PptStrategy().build_plan(
        goal="做一份图文并茂的 lifestyle deck 6 页",
        requested_slide_count=6,
        build_batch_size=2,
        default_create_file="deck.pptx",
        merged_constraints={"style_preset": "lifestyle"},
    )
    # At least one image_showcase slide exists and requires picture
    showcases = [s for s in plan["slides"] if s["content_subtype"] == "image_showcase"]
    assert showcases, "lifestyle preset should schedule image_showcase slides"
    for s in showcases:
        assert s["requires_real_picture"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k build_plan_adds_taxonomy_fields`
Expected: FAIL (KeyError: page_type)

- [ ] **Step 3: Extend slide schema**

Edit `agent/workflows/office/strategies/ppt.py:19-32`(the `slides = [...]` dict comprehension in `build_plan`):

```python
        preset_name = str((merged_constraints or {}).get("style_preset") or "business_formal")
        from agent.workflows.office.style_presets import get_preset

        preset = get_preset(preset_name)
        slides = []
        for idx in range(slide_count):
            index = idx + 1
            page_type = _slide_page_type(index, slide_count)
            content_subtype = _slide_content_subtype(index, slide_count, preset_name)
            requires_picture = _slide_requires_real_picture(page_type, content_subtype)
            slides.append(
                {
                    "index": index,
                    "title": slide_titles[idx],
                    "role": _slide_role(index, slide_count),
                    "section": _slide_section(index, slide_count),
                    "takeaway": _slide_takeaway(slide_titles[idx], index, slide_count),
                    "layout_type": _slide_layout_type(index, slide_count),
                    "visual_requirements": _slide_visual_requirements(index, slide_count),
                    "transition_required": index > 1,
                    "notes_required": index not in {1, slide_count},
                    "page_type": page_type,
                    "content_subtype": content_subtype,
                    "requires_real_picture": requires_picture,
                    "max_text_blocks": _slide_max_text_blocks(page_type, content_subtype),
                    "typography_pair": {"header_font": preset.typography.header, "body_font": preset.typography.body},
                    "theme_ref": f"{preset_name}:{_slide_theme_ref(page_type)}",
                }
            )
```

Add helper functions at the bottom of the module:

```python
def _slide_page_type(index: int, slide_count: int) -> str:
    if index == 1:
        return "cover"
    if index == 2 and slide_count >= 4:
        return "toc"
    if index == slide_count:
        return "summary"
    return "content"


def _slide_content_subtype(index: int, slide_count: int, preset_name: str) -> str | None:
    page_type = _slide_page_type(index, slide_count)
    if page_type != "content":
        return None
    rotation = ("text", "mixed", "data_viz", "comparison", "timeline", "image_showcase")
    if preset_name == "lifestyle":
        rotation = ("image_showcase", "mixed", "image_showcase", "timeline", "comparison", "text")
    return rotation[(index - 3) % len(rotation)]


def _slide_requires_real_picture(page_type: str, content_subtype: str | None) -> bool:
    if page_type == "cover":
        return True
    if content_subtype in {"image_showcase", "timeline", "comparison"}:
        return True
    return False


def _slide_max_text_blocks(page_type: str, content_subtype: str | None) -> int:
    if page_type in {"cover", "summary"}:
        return 2
    if content_subtype == "image_showcase":
        return 2
    if content_subtype in {"data_viz", "comparison"}:
        return 3
    return 4


def _slide_theme_ref(page_type: str) -> str:
    if page_type in {"cover", "summary"}:
        return "primary"
    if page_type == "toc":
        return "secondary"
    return "accent"
```

- [ ] **Step 4: Update validate_plan so new fields survive normalization**

Edit `agent/workflows/office/strategies/ppt.py:121-130` — the `required_keys` tuple. Add the new field names:

```python
        required_keys = (
            "title",
            "role",
            "section",
            "takeaway",
            "layout_type",
            "visual_requirements",
            "transition_required",
            "notes_required",
            "page_type",
            "content_subtype",
            "requires_real_picture",
            "max_text_blocks",
            "typography_pair",
            "theme_ref",
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k build_plan_adds_taxonomy_fields`
Expected: PASS (all three taxonomy tests)

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py tests/test_office_domain.py
git commit -m "feat(office): extend PPT slide schema with taxonomy + preset-aware fields"
```

---

## Task 7: Per-slide gate — requires_real_picture

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py::_apply_ground_truth_gates`
- Test: `tests/test_office_domain.py`

spec 第 5.2: `slide.requires_real_picture == True and per_slide_stats.picture_count == 0` → 单页 fail。

- [ ] **Step 1: Write the failing test**

```python
def test_per_slide_gate_requires_real_picture_fails_when_cover_has_no_picture() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    strategy = PptStrategy()
    plan = strategy.build_plan(
        goal="做 6 页商务 deck",
        requested_slide_count=6,
        build_batch_size=2,
        default_create_file="deck.pptx",
        merged_constraints={"style_preset": "business_formal"},
    )
    gt = GroundTruthStats(
        slide_count=6,
        per_slide=[SlidePhysicalStats(index=i, picture_count=(1 if i == 5 else 0), layout_signature=f"sig-{i}") for i in range(1, 7)],
        unique_font_families={"Microsoft YaHei"},
    )
    issues = strategy.evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=6),
        plan=plan,
        ground_truth=gt,
    )
    assert any("G2-requires-real-picture" in str(i.get("message", "")) and "slide 1" in str(i.get("message", "")) for i in issues)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k requires_real_picture_fails_when_cover`
Expected: FAIL

- [ ] **Step 3: Extend `_apply_ground_truth_gates`**

In `_apply_ground_truth_gates`, after existing G1 gates, add:

```python
        plan_slides = list((plan or {}).get("slides") or []) if plan else []
        per_slide_by_index = {s.index: s for s in ground_truth.per_slide}
        for slide in plan_slides:
            idx = int(slide.get("index", 0) or 0)
            if idx <= 0:
                continue
            gt_slide = per_slide_by_index.get(idx)
            if gt_slide is None:
                continue
            if bool(slide.get("requires_real_picture")) and gt_slide.picture_count == 0:
                issues.append(
                    {
                        "severity": "error",
                        "message": f"G2-requires-real-picture: slide {idx} 要求真图但 picture_count=0",
                    }
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k requires_real_picture_fails_when_cover`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py tests/test_office_domain.py
git commit -m "feat(office): add per-slide G2-requires-real-picture gate"
```

---

## Task 8: Per-slide gate — max_text_blocks

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py::_apply_ground_truth_gates`
- Test: `tests/test_office_domain.py`

spec 第 5.2: `per_slide_stats.text_box_count > slide.max_text_blocks` → 单页 fail。

- [ ] **Step 1: Write the failing test**

```python
def test_per_slide_gate_max_text_blocks_fails_when_too_many_textboxes() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    strategy = PptStrategy()
    plan = strategy.build_plan(
        goal="做 6 页商务 deck",
        requested_slide_count=6,
        build_batch_size=2,
        default_create_file="deck.pptx",
        merged_constraints={"style_preset": "business_formal"},
    )
    # force slide 3's text_box_count to exceed its max_text_blocks
    per_slide = [
        SlidePhysicalStats(index=i, picture_count=1 if i in {1, 6} else 0, text_box_count=(10 if i == 3 else 1), layout_signature=f"sig-{i}")
        for i in range(1, 7)
    ]
    gt = GroundTruthStats(slide_count=6, per_slide=per_slide, unique_font_families={"Microsoft YaHei"})
    issues = strategy.evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=6),
        plan=plan,
        ground_truth=gt,
    )
    assert any("G2-max-text-blocks" in str(i.get("message", "")) and "slide 3" in str(i.get("message", "")) for i in issues)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k max_text_blocks_fails_when_too_many`
Expected: FAIL

- [ ] **Step 3: Extend `_apply_ground_truth_gates`**

Inside the per-slide loop:

```python
            max_text_blocks = slide.get("max_text_blocks")
            if isinstance(max_text_blocks, int) and gt_slide.text_box_count > max_text_blocks:
                issues.append(
                    {
                        "severity": "error",
                        "message": (
                            f"G2-max-text-blocks: slide {idx} text_box_count={gt_slide.text_box_count} > max={max_text_blocks}"
                        ),
                    }
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k max_text_blocks_fails_when_too_many`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py tests/test_office_domain.py
git commit -m "feat(office): add per-slide G2-max-text-blocks gate"
```

---

## Task 9: Per-slide gate — font family subset of typography_pair

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py::_apply_ground_truth_gates`
- Test: `tests/test_office_domain.py`

spec 第 5.2: `per_slide_stats.font_families not ⊆ plan.typography_pair` → 单页 fail。

- [ ] **Step 1: Write the failing test**

```python
def test_per_slide_gate_font_family_subset_fails_on_foreign_font() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    strategy = PptStrategy()
    plan = strategy.build_plan(
        goal="做 4 页商务 deck",
        requested_slide_count=4,
        build_batch_size=2,
        default_create_file="deck.pptx",
        merged_constraints={"style_preset": "business_formal"},
    )
    per_slide = []
    for i in range(1, 5):
        s = SlidePhysicalStats(index=i, picture_count=1, layout_signature=f"sig-{i}")
        if i == 2:
            s.font_families = {"Calibri"}
        else:
            s.font_families = {"Microsoft YaHei"}
        per_slide.append(s)
    gt = GroundTruthStats(
        slide_count=4,
        per_slide=per_slide,
        unique_font_families={"Microsoft YaHei", "Calibri"},
    )
    issues = strategy.evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=4),
        plan=plan,
        ground_truth=gt,
    )
    assert any("G2-font-subset" in str(i.get("message", "")) and "slide 2" in str(i.get("message", "")) for i in issues)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k font_family_subset_fails`
Expected: FAIL

- [ ] **Step 3: Extend `_apply_ground_truth_gates`**

Inside the per-slide loop:

```python
            typography = slide.get("typography_pair") or {}
            allowed = {str(typography.get("header_font") or ""), str(typography.get("body_font") or "")}
            allowed = {f for f in allowed if f}
            foreign = gt_slide.font_families - allowed
            if allowed and foreign:
                issues.append(
                    {
                        "severity": "error",
                        "message": f"G2-font-subset: slide {idx} 出现非法字体 {sorted(foreign)}，允许={sorted(allowed)}",
                    }
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k font_family_subset_fails`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py tests/test_office_domain.py
git commit -m "feat(office): add per-slide G2-font-subset gate"
```

---

## Task 10: 把 style_preset 和 theme / typography 注入 system prompt

**Files:**
- Modify: `agent/workflows/office/workflow.py::_build_format_specific_guidance`
- Modify: `agent/workflows/office/builder.py` (or wherever format_specific_guidance is called) 传 style_preset
- Test: `tests/test_office_workflow_prompt.py`

spec 第 5.3 Preset 用途 1: 注入 `_OFFICE_SYSTEM` 作为视觉合同("你本次只能用这 5 个 theme color、这 2 种字体")。

- [ ] **Step 1: Trace guidance call site**

Run: `grep -n "_build_format_specific_guidance" agent/workflows/office/`. Result: workflow.py:694-699 in `build_node`.

- [ ] **Step 2: Write the failing test**

```python
def test_format_guidance_injects_style_preset_theme_and_fonts() -> None:
    from agent.workflows.office.workflow import _build_format_specific_guidance

    guidance = _build_format_specific_guidance(
        goal="做商务 deck",
        format_name="pptx",
        operation="create",
        requested_slide_count=8,
        style_preset="business_formal",
    )
    assert "1F3864" in guidance  # primary
    assert "Microsoft YaHei" in guidance
    assert "business_formal" in guidance


def test_format_guidance_falls_back_to_business_formal_when_no_preset() -> None:
    from agent.workflows.office.workflow import _build_format_specific_guidance

    guidance = _build_format_specific_guidance(
        goal="random deck",
        format_name="pptx",
        operation="create",
        requested_slide_count=6,
    )
    assert "1F3864" in guidance
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_office_workflow_prompt.py -v -k format_guidance_injects_style_preset`
Expected: FAIL (signature doesn't accept style_preset)

- [ ] **Step 4: Extend `_build_format_specific_guidance`**

Edit `agent/workflows/office/workflow.py:172-235`:

```python
def _build_format_specific_guidance(
    *,
    goal: str,
    format_name: str,
    operation: str,
    requested_slide_count: int | None,
    style_preset: str | None = None,
) -> str:
    if format_name != "pptx" or operation not in {"create", "transform"}:
        return ""

    from agent.workflows.office.style_presets import get_preset

    preset = get_preset(style_preset)
    theme_line = (
        f"- 本次使用 style_preset = `{preset.name}`。"
        f"只允许使用下列 5 个主题色 (hex)："
        f"primary={preset.theme['primary']}, secondary={preset.theme['secondary']}, "
        f"accent={preset.theme['accent']}, light={preset.theme['light']}, bg={preset.theme['bg']}。"
    )
    font_line = (
        f"- 字体只允许 header={preset.typography.header} / body={preset.typography.body}。"
        "禁止出现 Calibri / Georgia / Cambria。"
    )

    # ... (preserve existing logic for slide_count_rule, storyline_hint)
    # Prepend theme_line + font_line to the existing block.
```

Insert `theme_line` and `font_line` near the top of the returned guidance block, just below `## PPT 创建质量门槛`.

- [ ] **Step 5: Update call site in build_node**

Edit `agent/workflows/office/workflow.py:694-699`:

```python
    format_specific_guidance = _build_format_specific_guidance(
        goal=str(state.get("goal", "") or ""),
        format_name=format_hint if format_hint != "auto" else "",
        operation=operation,
        requested_slide_count=requested_slide_count,
        style_preset=str(state.get("style_preset") or (state.get("task_profile") or {}).get("style_preset") or ""),
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_office_workflow_prompt.py -v -k format_guidance_injects_style_preset`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add agent/workflows/office/workflow.py tests/test_office_workflow_prompt.py
git commit -m "feat(office): inject style preset theme+fonts into format guidance"
```

---

## Task 11: build_plan consumes style_preset from state via merged_constraints

**Files:**
- Modify: `agent/workflows/office/workflow.py::planning_node`
- Test: `tests/test_office_domain.py`

spec 第 5.3 Preset 用途 2: `build_plan` 使用 preset 的 `layout_rotation` 生成每页 `layout_type`。Task 6 已支持通过 `merged_constraints["style_preset"]`。此 task 确保 planning_node 把 style_preset 透传给 merged_constraints。

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_planning_node_passes_style_preset_into_merged_constraints() -> None:
    from agent.workflows.office.workflow import planning_node

    result = await planning_node({
        "goal": "营销推广 6 页 deck",
        "format": "pptx",
        "operation": "create",
        "requested_slide_count": 6,
        "build_batch_size": 2,
        "default_create_file": "m.pptx",
        "style_preset": "marketing",
        "task_profile": {"style_preset": "marketing"},
    })

    merged = result.get("task_profile", {}).get("merged_constraints") or {}
    assert merged.get("style_preset") == "marketing"
    first_slide = result["deck_plan"]["slides"][0]
    assert first_slide["theme_ref"].startswith("marketing:")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k planning_node_passes_style_preset`
Expected: FAIL (merged_constraints has no style_preset)

- [ ] **Step 3: Extend planning_node**

Edit `agent/workflows/office/workflow.py:613-632` — the `goal_constraints = {...}` dict passed to `resolve_reference_constraints`. Inject style_preset:

```python
    style_preset = str(state.get("style_preset") or (state.get("task_profile") or {}).get("style_preset") or "")
    merged_constraints = resolve_reference_constraints(
        goal_constraints={
            **goal_constraints,
            "format": strategy_format,
            "operation": str(state.get("operation", "") or ""),
            "goal": str(state.get("goal", "") or ""),
            "style_preset": style_preset or None,
        },
        reference_structure_constraints={...},
        reference_style_constraints={...},
        existing_document_profile={...},
    )
    if style_preset:
        merged_constraints = {**merged_constraints, "style_preset": style_preset}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k planning_node_passes_style_preset`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/workflow.py tests/test_office_domain.py
git commit -m "feat(office): thread style_preset through planning_node merged_constraints"
```

---

## Task 12: 全 regression + preset sample decks smoke

- [ ] **Step 1: Full office test run**

Run: `pytest tests/test_office_domain.py tests/test_office_workflow_prompt.py tests/test_office_goal_normalizer.py tests/test_style_presets.py tests/test_ppt_stats_reader.py -v`
Expected: all PASS

- [ ] **Step 2: Manual 5-preset smoke**

For each preset(business_formal / marketing / product_launch / course_training / lifestyle), run the agent with a representative goal:

| Preset | Goal |
|---|---|
| business_formal | 给董事会做 Q3 业绩汇报,8 页 |
| marketing | 营销推广 campaign deck,10 页 |
| product_launch | 产品发布 launch deck,8 页 |
| course_training | 新员工培训 workshop,8 页 |
| lifestyle | 钓鱼好处的生活方式分享,6 页,要配图 |

Verify for each:
  - QA passes on first or second build(≤1 repair round)
  - 真机打开 pptx 检查字体 = YaHei,色板与 preset 一致,cover 有 picture

- [ ] **Step 3: No commit — record observations**

记录 preset vs. first-round-pass ratio。若有任一 preset 连续两轮都失败,把原因追加到本 plan 的 Rollout Notes。

---

## Self-Review Checklist

- [ ] Spec §5.1 Page taxonomy: Task 6 adds all 6 new slide fields(page_type/content_subtype/requires_real_picture/max_text_blocks/typography_pair/theme_ref)。 ✅
- [ ] Spec §5.2 Per-slide QA: Tasks 7-9 add 3 per-slide gates(G2-*)。 ✅
- [ ] Spec §5.2.1 色板门槛延后: not included — spec 明确"批 1 后再评估",Batch 2 不做。 ✅
- [ ] Spec §5.3 Style preset 系统: Task 1 创建 5 preset。Task 10-11 注入 prompt 与 build_plan。 ✅
- [ ] Spec §5.4 Vertical 推断 + interrupt: Tasks 3-5 实现 infer + wire + interrupt。 ✅
- [ ] Spec §5.5 acceptance: Task 12 手动 5-preset smoke。 ✅
- [ ] Spec §7 state 字段: Task 5 加 `style_preset`。 ✅
- [ ] Type names consistent: `StylePreset`, `infer_style_preset`, `_apply_ground_truth_gates`, `GroundTruthStats`, `SlidePhysicalStats`, `get_preset` — confirmed across tasks。 ✅
- [ ] No TBD / TODO / placeholder code. ✅

---

## Rollout Notes

- Batch 2 必须和 Batch 1 一起使用 — 独立部署 Batch 2 不合理(Task 7-9 依赖 Batch 1 的 GroundTruthStats)。
- 5 preset 的 theme hex 和 corner_radius 是设计决定,上线后按 benchmark 结果迭代。
- `infer_style_preset` 只做 keyword-based,未接 LLM extract — spec 5.4 提过 LLM 路径,留作后续优化(YAGNI)。
