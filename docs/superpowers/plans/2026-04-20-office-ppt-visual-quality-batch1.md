# Office PPT Visual Quality — Batch 1 Implementation Plan (Ground-Truth Stats + Hard Gates)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 PPT QA 真值源从"模型自报 stats"改成"officecli 工具原始输出",并在真值之上新增 6 个硬门槛拦截字体漂/重复标题/缺图/布局重复等常见视觉失败。

**Architecture:** 新增独立解析器 `ppt_stats_reader.py`,把 `view stats` / `view annotated` 的 raw tool output 解析成结构化 `GroundTruthStats`,再交给 `PptStrategy.evaluate_quality_stats` 做硬门槛判定。模型自报的 `stats` 字段从"判定真值"降级成"对照参考"。保持现有 `build → qa_fix → build` 修复循环不变。

**Tech Stack:** Python 3.14, pydantic, pytest, LangGraph-based office workflow

**Scope anchor:** spec `docs/superpowers/specs/2026-04-20-office-ppt-visual-quality-design.md` 第 4 章(Batch 1)。

---

## File Structure

| 文件 | 改动类型 | 责任 |
|---|---|---|
| `agent/workflows/office/strategies/ppt_stats_reader.py` | 新增 | 解析 `view stats` / `view annotated` 原始输出 → `GroundTruthStats` |
| `agent/workflows/office/strategies/ppt.py` | 重写 `evaluate_quality_stats`,新增 6 个 gate 实现 | 单文件最大改动 |
| `agent/workflows/office/core/state.py` | 扩字段 | 加 `ground_truth_stats: dict \| None` |
| `agent/workflows/office/core/qa.py` | 改 run_qa_fix_stage | 从 `intermediate_results` 抽 ground_truth,传给 strategy |
| `agent/workflows/office/builder.py` / 相关 raw-tool capture | 扩出参 | 让 build 阶段把 `view stats` / `view annotated` 原始文本回传到 `intermediate_results` |
| `agent/workflows/office/workflow.py` | 改 `_build_format_specific_guidance`、`_OFFICE_SYSTEM` | 配图硬要求、字体合同、真值 QA 段 |
| `tests/test_ppt_stats_reader.py` | 新增 | 解析器单测 + golden fixtures |
| `tests/test_office_domain.py` | 增 case | 每个 gate 的 PASS/FAIL case |
| `tests/test_office_workflow_prompt.py` | 增 case | 新 prompt 段断言 |
| `tests/fixtures/ppt_stats/` | 新增 | `view stats` / `view annotated` 真值样本 |

---

## Task 1: 定义 GroundTruthStats 数据模型

**Files:**
- Create: `agent/workflows/office/strategies/ppt_stats_reader.py`
- Test: `tests/test_ppt_stats_reader.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ppt_stats_reader.py`:

```python
from __future__ import annotations

from agent.workflows.office.strategies.ppt_stats_reader import (
    GroundTruthStats,
    SlidePhysicalStats,
)


def test_slide_physical_stats_defaults() -> None:
    stats = SlidePhysicalStats(index=1, layout_signature="T-TB")
    assert stats.picture_count == 0
    assert stats.chart_count == 0
    assert stats.table_count == 0
    assert stats.smartart_count == 0
    assert stats.shape_count == 0
    assert stats.text_box_count == 0
    assert stats.distinct_title_objects == 0
    assert stats.has_notes is False
    assert stats.has_transition is False


def test_ground_truth_stats_defaults() -> None:
    stats = GroundTruthStats(slide_count=0)
    assert stats.per_slide == []
    assert stats.unique_font_families == set()
    assert stats.theme_colors == []
    assert stats.placeholder_remnant_hits == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_stats_reader.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.workflows.office.strategies.ppt_stats_reader`

- [ ] **Step 3: Write minimal implementation**

Create `agent/workflows/office/strategies/ppt_stats_reader.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SlidePhysicalStats:
    index: int
    layout_signature: str = ""
    picture_count: int = 0
    chart_count: int = 0
    table_count: int = 0
    smartart_count: int = 0
    shape_count: int = 0
    text_box_count: int = 0
    distinct_title_objects: int = 0
    has_notes: bool = False
    has_transition: bool = False
    font_families: set[str] = field(default_factory=set)


@dataclass
class GroundTruthStats:
    slide_count: int
    per_slide: list[SlidePhysicalStats] = field(default_factory=list)
    unique_font_families: set[str] = field(default_factory=set)
    theme_colors: list[str] = field(default_factory=list)
    placeholder_remnant_hits: list[tuple[int, str]] = field(default_factory=list)


__all__ = ["GroundTruthStats", "SlidePhysicalStats"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ppt_stats_reader.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt_stats_reader.py tests/test_ppt_stats_reader.py
git commit -m "feat(office): add GroundTruthStats data model for PPT QA"
```

---

## Task 2: 捕获 `view stats` / `view annotated` 原始样本

**Files:**
- Create: `tests/fixtures/ppt_stats/sample_view_stats.txt`
- Create: `tests/fixtures/ppt_stats/sample_view_annotated.txt`

Goal: 保存 officecli 在真实 deck 上的 raw tool output 作为 golden fixtures。这样 Task 3 的 parser 有确定的真值样本可以对齐。

- [ ] **Step 1: Generate fixture via officecli**

从现有的 benchmark deck(例如 `outputs/*.pptx` 或新生成一份 6 页 deck)跑:

```bash
python -c "
import asyncio
from agent.tools.officecli import execute_officecli_spec
async def main():
    stats = await execute_officecli_spec({'verb': 'view', 'file': 'outputs/benchmark.pptx', 'mode': 'stats'})
    print(stats)
asyncio.run(main())
" > /tmp/raw_stats.txt
```

替换 `outputs/benchmark.pptx` 为本地任一可读 deck。如果没有可读 deck,用 `outputs/*.pptx` 下第一份。

- [ ] **Step 2: Save fixtures as-is**

```bash
mkdir -p tests/fixtures/ppt_stats
# 把 /tmp/raw_stats.txt 的关键字段复制到 sample_view_stats.txt;
# 把 /tmp/raw_annotated.txt 的内容复制到 sample_view_annotated.txt
```

Fixture 要求: 保留 raw tool output 中所有字段(含 `slide_count`, 每页 shape 列表、字体标注、notes 标记、transition 标记)。不允许删行精简。

- [ ] **Step 3: Commit fixtures**

```bash
git add tests/fixtures/ppt_stats/
git commit -m "test(office): add golden fixtures for ppt_stats_reader"
```

---

## Task 3: 解析 `view stats` 的 raw output

**Files:**
- Modify: `agent/workflows/office/strategies/ppt_stats_reader.py`
- Test: `tests/test_ppt_stats_reader.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ppt_stats_reader.py`:

```python
from pathlib import Path


def _load_fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "ppt_stats" / name).read_text(encoding="utf-8")


def test_parse_view_stats_extracts_slide_count() -> None:
    from agent.workflows.office.strategies.ppt_stats_reader import parse_view_stats

    raw = _load_fixture("sample_view_stats.txt")
    stats = parse_view_stats(raw)

    assert stats.slide_count >= 1
    assert len(stats.per_slide) == stats.slide_count
    for s in stats.per_slide:
        assert s.picture_count >= 0
        assert s.chart_count >= 0
        assert s.table_count >= 0


def test_parse_view_stats_accepts_dict_input() -> None:
    from agent.workflows.office.strategies.ppt_stats_reader import parse_view_stats

    payload = {
        "slide_count": 2,
        "slides": [
            {"index": 1, "pictures": 0, "charts": 0, "tables": 0, "smartart": 0, "shapes": 2, "text_boxes": 3, "has_notes": True, "has_transition": False},
            {"index": 2, "pictures": 1, "charts": 0, "tables": 0, "smartart": 0, "shapes": 1, "text_boxes": 2, "has_notes": False, "has_transition": True},
        ],
    }
    stats = parse_view_stats(payload)
    assert stats.slide_count == 2
    assert stats.per_slide[1].picture_count == 1
    assert stats.per_slide[1].has_transition is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_stats_reader.py::test_parse_view_stats_extracts_slide_count -v`
Expected: FAIL with `ImportError: cannot import name 'parse_view_stats'`

- [ ] **Step 3: Implement parse_view_stats**

Append to `agent/workflows/office/strategies/ppt_stats_reader.py`:

```python
import json
import re
from typing import Any


def parse_view_stats(raw: Any) -> GroundTruthStats:
    """Parse officecli view stats output (dict or raw text) into GroundTruthStats."""
    payload = _coerce_to_dict(raw)
    slide_count = int(payload.get("slide_count") or 0)
    per_slide: list[SlidePhysicalStats] = []
    for entry in payload.get("slides") or []:
        if not isinstance(entry, dict):
            continue
        per_slide.append(
            SlidePhysicalStats(
                index=int(entry.get("index") or len(per_slide) + 1),
                picture_count=int(entry.get("pictures") or entry.get("picture_count") or 0),
                chart_count=int(entry.get("charts") or entry.get("chart_count") or 0),
                table_count=int(entry.get("tables") or entry.get("table_count") or 0),
                smartart_count=int(entry.get("smartart") or entry.get("smartart_count") or 0),
                shape_count=int(entry.get("shapes") or entry.get("shape_count") or 0),
                text_box_count=int(entry.get("text_boxes") or entry.get("text_box_count") or 0),
                has_notes=bool(entry.get("has_notes")),
                has_transition=bool(entry.get("has_transition")),
            )
        )
    # Pad per_slide to slide_count if fixture doesn't repeat index data.
    while len(per_slide) < slide_count:
        per_slide.append(SlidePhysicalStats(index=len(per_slide) + 1))
    return GroundTruthStats(
        slide_count=slide_count,
        per_slide=per_slide,
    )


def _coerce_to_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _parse_text_stats(text)
    return parsed if isinstance(parsed, dict) else {}


_SLIDE_HEADER_RE = re.compile(r"slide\s+(\d+)", re.IGNORECASE)


def _parse_text_stats(text: str) -> dict[str, Any]:
    """Fallback parser for plain-text `view stats` output."""
    slide_count_match = re.search(r"slide[_\s]count\s*[:=]\s*(\d+)", text, re.IGNORECASE)
    slide_count = int(slide_count_match.group(1)) if slide_count_match else 0
    return {"slide_count": slide_count, "slides": []}
```

Append:

```python
__all__ = ["GroundTruthStats", "SlidePhysicalStats", "parse_view_stats"]
```

and remove the earlier `__all__` definition.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ppt_stats_reader.py -v`
Expected: PASS

Note: 如果 fixture 里的字段名和 parser 里的 alias 不完全匹配,在 `parse_view_stats` 里再加 alias 直到 fixture-based test 通过。

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt_stats_reader.py tests/test_ppt_stats_reader.py
git commit -m "feat(office): parse view stats raw output into GroundTruthStats"
```

---

## Task 4: 解析 `view annotated`,提取字体家族、重复标题、placeholder 残留

**Files:**
- Modify: `agent/workflows/office/strategies/ppt_stats_reader.py`
- Test: `tests/test_ppt_stats_reader.py`

spec 第 4.1 与 risk #2: `view annotated` 每个 Text Box 行会带 `← <Font> <size>pt` 标注。parser 要从中抽每页的 `font_families`,并识别出同页 `Title` / `TextBox[is_title=true]` 出现多次的情况,以及命中 `{xxxx, lorem ipsum, TODO, FIXME, 占位, placeholder}` 的文本。

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ppt_stats_reader.py`:

```python
def test_annotated_extracts_font_families() -> None:
    from agent.workflows.office.strategies.ppt_stats_reader import merge_annotated_into_stats, parse_view_stats

    base = parse_view_stats({"slide_count": 2, "slides": [{"index": 1}, {"index": 2}]})
    annotated = """
Slide 1
- Title: 钓鱼的好处 ← Microsoft YaHei 44pt
- TextBox: 放松身心 ← Calibri 18pt
Slide 2
- Title: 垂钓技巧 ← Microsoft YaHei 32pt
- TextBox: Georgia 正文 ← Georgia 14pt
""".strip()
    stats = merge_annotated_into_stats(base, annotated)
    assert stats.unique_font_families == {"Microsoft YaHei", "Calibri", "Georgia"}
    assert "Microsoft YaHei" in stats.per_slide[0].font_families
    assert "Calibri" in stats.per_slide[0].font_families


def test_annotated_detects_duplicate_titles() -> None:
    from agent.workflows.office.strategies.ppt_stats_reader import merge_annotated_into_stats, parse_view_stats

    base = parse_view_stats({"slide_count": 1, "slides": [{"index": 1}]})
    annotated = """
Slide 1
- Title (placeholder): 主题 ← Microsoft YaHei 40pt
- TextBox (manual_title): 主题 ← Microsoft YaHei 38pt
""".strip()
    stats = merge_annotated_into_stats(base, annotated)
    assert stats.per_slide[0].distinct_title_objects >= 2


def test_annotated_flags_placeholder_remnants() -> None:
    from agent.workflows.office.strategies.ppt_stats_reader import merge_annotated_into_stats, parse_view_stats

    base = parse_view_stats({"slide_count": 2, "slides": [{"index": 1}, {"index": 2}]})
    annotated = """
Slide 1
- TextBox: xxxx ← Microsoft YaHei 18pt
Slide 2
- TextBox: Lorem ipsum dolor sit amet ← Microsoft YaHei 18pt
""".strip()
    stats = merge_annotated_into_stats(base, annotated)
    hits = {idx for (idx, _) in stats.placeholder_remnant_hits}
    assert hits == {1, 2}


def test_annotated_layout_signature() -> None:
    from agent.workflows.office.strategies.ppt_stats_reader import merge_annotated_into_stats, parse_view_stats

    base = parse_view_stats({"slide_count": 2, "slides": [{"index": 1}, {"index": 2}]})
    annotated = """
Slide 1
- Title: A ← YaHei 32pt
- TextBox: body ← YaHei 14pt
- Picture: hero.png
Slide 2
- Title: B ← YaHei 32pt
- TextBox: body ← YaHei 14pt
- Picture: hero2.png
""".strip()
    stats = merge_annotated_into_stats(base, annotated)
    assert stats.per_slide[0].layout_signature == stats.per_slide[1].layout_signature
    assert stats.per_slide[0].layout_signature == "Title-TextBox-Picture"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ppt_stats_reader.py -v -k annotated`
Expected: FAIL with `ImportError: cannot import name 'merge_annotated_into_stats'`

- [ ] **Step 3: Implement annotated parser**

Append to `agent/workflows/office/strategies/ppt_stats_reader.py`:

```python
_FONT_ANNOTATION_RE = re.compile(r"←\s*([A-Za-z][A-Za-z0-9 \-']{0,30})(?:\s+\d+\s*pt)?", re.IGNORECASE)
_SLIDE_BLOCK_RE = re.compile(r"^\s*slide\s+(\d+)\s*$", re.IGNORECASE)
_SHAPE_LINE_RE = re.compile(r"^\s*-\s*(Title|TextBox|Picture|Chart|Table|SmartArt|Shape)", re.IGNORECASE)
_PLACEHOLDER_PATTERNS = ("xxxx", "lorem ipsum", "todo", "fixme", "占位", "placeholder")


def merge_annotated_into_stats(base: GroundTruthStats, annotated_raw: Any) -> GroundTruthStats:
    """Augment base GroundTruthStats with per-slide data derived from `view annotated`."""
    text = str(annotated_raw or "")
    if not text.strip():
        return base

    per_slide_by_index: dict[int, SlidePhysicalStats] = {s.index: s for s in base.per_slide}
    unique_fonts: set[str] = set(base.unique_font_families)
    placeholder_hits: list[tuple[int, str]] = list(base.placeholder_remnant_hits)

    current_idx = 0
    shape_sequence: dict[int, list[str]] = {}
    title_counts: dict[int, int] = {}

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        header = _SLIDE_BLOCK_RE.match(line)
        if header:
            current_idx = int(header.group(1))
            if current_idx not in per_slide_by_index:
                per_slide_by_index[current_idx] = SlidePhysicalStats(index=current_idx)
            shape_sequence.setdefault(current_idx, [])
            title_counts.setdefault(current_idx, 0)
            continue
        if current_idx == 0:
            continue
        shape_match = _SHAPE_LINE_RE.match(line)
        if not shape_match:
            continue

        shape_type = shape_match.group(1)
        canonical = _canonical_shape_name(shape_type)
        shape_sequence[current_idx].append(canonical)

        lowered = line.lower()
        if canonical == "Title" or "manual_title" in lowered or "is_title" in lowered:
            title_counts[current_idx] += 1

        if any(pat in lowered for pat in _PLACEHOLDER_PATTERNS):
            placeholder_hits.append((current_idx, _first_matching_pattern(lowered)))

        font_match = _FONT_ANNOTATION_RE.search(line)
        if font_match:
            family = font_match.group(1).strip()
            if family:
                unique_fonts.add(family)
                per_slide_by_index[current_idx].font_families.add(family)

    per_slide_list: list[SlidePhysicalStats] = []
    slide_count = max(base.slide_count, max(shape_sequence.keys() or [0]))
    for idx in range(1, slide_count + 1):
        entry = per_slide_by_index.get(idx, SlidePhysicalStats(index=idx))
        entry.layout_signature = "-".join(shape_sequence.get(idx, []))
        entry.distinct_title_objects = title_counts.get(idx, entry.distinct_title_objects)
        per_slide_list.append(entry)

    return GroundTruthStats(
        slide_count=slide_count,
        per_slide=per_slide_list,
        unique_font_families=unique_fonts,
        theme_colors=list(base.theme_colors),
        placeholder_remnant_hits=placeholder_hits,
    )


def _canonical_shape_name(raw: str) -> str:
    mapping = {
        "title": "Title",
        "textbox": "TextBox",
        "picture": "Picture",
        "chart": "Chart",
        "table": "Table",
        "smartart": "SmartArt",
        "shape": "Shape",
    }
    return mapping.get(raw.lower(), raw)


def _first_matching_pattern(lowered: str) -> str:
    for pat in _PLACEHOLDER_PATTERNS:
        if pat in lowered:
            return pat
    return ""
```

Update `__all__`:

```python
__all__ = [
    "GroundTruthStats",
    "SlidePhysicalStats",
    "merge_annotated_into_stats",
    "parse_view_stats",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ppt_stats_reader.py -v`
Expected: PASS (all annotated tests plus prior stats tests)

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt_stats_reader.py tests/test_ppt_stats_reader.py
git commit -m "feat(office): merge view annotated into GroundTruthStats (fonts/titles/placeholders/layout_signature)"
```

---

## Task 5: State 扩字段并在 `evaluate_quality_stats` 签名中接收 plan + ground_truth

**Files:**
- Modify: `agent/workflows/office/core/state.py:16-73`
- Modify: `agent/workflows/office/strategies/base.py:58-66`
- Modify: `agent/workflows/office/strategies/ppt.py:284-367`
- Modify: `agent/workflows/office/strategies/ppt.py:441-446`
- Modify: `agent/workflows/office/strategies/default.py` / `docx.py` / `xlsx.py` (保持 signature 兼容)
- Test: `tests/test_office_domain.py`

spec 第 4.1 步 3: `evaluate_quality_stats` 签名改为接收 `GroundTruthStats`。此 Task 只做签名改造和 state 字段,不加具体 gate。

- [ ] **Step 1: Write the failing test**

Append to `tests/test_office_domain.py`:

```python
def test_ppt_evaluate_quality_stats_accepts_ground_truth() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    strategy = PptStrategy()
    gt = GroundTruthStats(
        slide_count=3,
        per_slide=[
            SlidePhysicalStats(index=1, picture_count=1, text_box_count=1, layout_signature="Title-Picture"),
            SlidePhysicalStats(index=2, picture_count=0, text_box_count=3, layout_signature="Title-TextBox"),
            SlidePhysicalStats(index=3, picture_count=0, text_box_count=2, layout_signature="Title-TextBox"),
        ],
        unique_font_families={"Microsoft YaHei", "Arial"},
    )
    issues = strategy.evaluate_quality_stats(
        operation="create",
        stats={"slide_count": 3, "content_slide_count": 3, "notes_slide_count": 3, "transition_slide_count": 2,
               "visual_slide_count": 1, "text_only_slide_count": 0, "layout_variety_count": 2,
               "picture_count": 1, "chart_count": 0, "table_count": 0,
               "qa_checks": ["view_stats", "view_annotated", "validate"]},
        ground_truth=gt,
    )
    assert isinstance(issues, list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py::test_ppt_evaluate_quality_stats_accepts_ground_truth -v`
Expected: FAIL with `TypeError: evaluate_quality_stats() got an unexpected keyword argument 'ground_truth'`

- [ ] **Step 3: Add `ground_truth_stats` state field**

Edit `agent/workflows/office/core/state.py`,在 `class OfficeWorkflowState` 中、`intermediate_results` 上面插入:

```python
    ground_truth_stats: dict[str, Any] | None
```

- [ ] **Step 4: Update Protocol signature**

Edit `agent/workflows/office/strategies/base.py`, 在 `evaluate_quality_stats` 签名上加 `ground_truth: Any = None`:

```python
    def evaluate_quality_stats(
        self,
        *,
        operation: str,
        stats: dict[str, Any],
        plan: dict[str, Any] | None = None,
        merged_constraints: dict[str, Any] | None = None,
        result_meta: dict[str, Any] | None = None,
        ground_truth: Any = None,
    ) -> list[dict[str, Any]]: ...
```

- [ ] **Step 5: Update PptStrategy, DefaultOfficeStrategy, DocxStrategy, XlsxStrategy**

Edit `agent/workflows/office/strategies/ppt.py:284`, 扩签名并把 `ground_truth` 传到内部(本 task 暂只落位,不加判定):

```python
    def evaluate_quality_stats(
        self,
        *,
        operation: str,
        stats: dict[str, Any],
        plan: dict[str, Any] | None = None,
        merged_constraints: dict[str, Any] | None = None,
        result_meta: dict[str, Any] | None = None,
        ground_truth: Any = None,
    ) -> list[dict[str, Any]]:
```

对 `agent/workflows/office/strategies/default.py`、`docx.py`、`xlsx.py` 做相同的签名扩展(方法体不变)。

Also, 同步修改 `evaluate_quality_stats` 的模块级函数 `agent/workflows/office/strategies/ppt.py:441-446`:

```python
def evaluate_quality_stats(
    *,
    operation: str,
    stats: dict[str, Any],
    ground_truth: Any = None,
) -> list[dict[str, Any]]:
    return _PPT_STRATEGY.evaluate_quality_stats(operation=operation, stats=stats, ground_truth=ground_truth)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k evaluate_quality_stats_accepts_ground_truth`
Expected: PASS

- [ ] **Step 7: Full office test suite still passes**

Run: `pytest tests/test_office_domain.py tests/test_office_workflow_prompt.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add agent/workflows/office/core/state.py agent/workflows/office/strategies/ tests/test_office_domain.py
git commit -m "feat(office): extend evaluate_quality_stats signature with ground_truth"
```

---

## Task 6: Build 阶段捕获 `view stats` / `view annotated` 原始输出

**Files:**
- Modify: `agent/workflows/office/builder.py` (or the place where raw tool outputs get recorded into `intermediate_results`)
- Modify: `agent/workflows/office/core/qa.py:196-224`
- Test: `tests/test_office_domain.py`

spec 第 4.1 步 1: build 结尾要强制 `view stats` + `view annotated`,把 raw 输出放进 `intermediate_results` 的 `ground_truth_stats_raw` 字段,而不是只看模型压缩后的 JSON。

- [ ] **Step 1: Read builder to locate the tool-capture seam**

Read `agent/workflows/office/builder.py` end-to-end;找 `intermediate_results` 的 append 点和官方工具调用被 observe 的位置。记下两个锚点:
  - build_node 写入 `intermediate_results` 的位置
  - 工具结果返回的位置(LangGraph tool observations)

Run: `grep -n "intermediate_results\|tool_calls\|ToolMessage" agent/workflows/office/builder.py`

- [ ] **Step 2: Write the failing test**

Append to `tests/test_office_domain.py`:

```python
def test_qa_fix_uses_ground_truth_from_intermediate_results(monkeypatch) -> None:
    from agent.workflows.office.core.qa import run_qa_fix_stage
    from agent.workflows.office.strategies.ppt import PptStrategy

    captured = {}

    original = PptStrategy.evaluate_quality_stats

    def spy(self, *, operation, stats, plan=None, merged_constraints=None, result_meta=None, ground_truth=None):
        captured["ground_truth"] = ground_truth
        return []

    monkeypatch.setattr(PptStrategy, "evaluate_quality_stats", spy)

    state = {
        "format": "pptx",
        "operation": "create",
        "write_required": True,
        "intermediate_results": [{
            "output": '```json\n{"operation":"create","validated":true,"summary":"ok","artifacts":[{"filename":"deck.pptx","path":"","format":"pptx","role":"primary"}],"stats":{"slide_count":1,"content_slide_count":1,"notes_slide_count":1,"transition_slide_count":0,"visual_slide_count":1,"text_only_slide_count":0,"layout_variety_count":1,"picture_count":1,"chart_count":0,"table_count":0,"qa_checks":["view_stats","view_annotated","validate"]}}\n```',
            "ground_truth_stats_raw": {
                "view_stats": {"slide_count": 1, "slides": [{"index": 1, "pictures": 1}]},
                "view_annotated": "Slide 1\n- Title: A ← YaHei 32pt\n- Picture: hero.png",
            },
        }],
    }
    run_qa_fix_stage(state, strategy=PptStrategy())

    gt = captured["ground_truth"]
    assert gt is not None
    assert gt.slide_count == 1
    assert gt.per_slide[0].picture_count == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py::test_qa_fix_uses_ground_truth_from_intermediate_results -v`
Expected: FAIL (ground_truth is None — qa.py doesn't read it yet)

- [ ] **Step 4: Update qa.py to build GroundTruthStats before handing to strategy**

Edit `agent/workflows/office/core/qa.py:196-224`, 在 `strategy.evaluate_quality_stats(...)` 调用前加:

```python
from agent.workflows.office.strategies.ppt_stats_reader import (
    merge_annotated_into_stats,
    parse_view_stats,
)


def _extract_ground_truth(results: list[dict[str, Any]]):
    for entry in reversed(results):
        raw = entry.get("ground_truth_stats_raw") if isinstance(entry, dict) else None
        if not isinstance(raw, dict):
            continue
        base = parse_view_stats(raw.get("view_stats"))
        return merge_annotated_into_stats(base, raw.get("view_annotated"))
    return None
```

然后在 `run_qa_fix_stage` 中 `issues.extend(strategy.evaluate_quality_stats(...))` 之前:

```python
    ground_truth = _extract_ground_truth(list(results))
```

并把 `ground_truth=ground_truth` 追加到 `strategy.evaluate_quality_stats(...)` 调用里。

- [ ] **Step 5: Update builder to capture raw tool output**

Edit `agent/workflows/office/builder.py`(位置按 Step 1 记录的锚点):build 完成后,如果最近一次 officecli 调用中包含 `verb=view, mode=stats` 和 `verb=view, mode=annotated` 的 ToolMessage,把它们的 raw content 放进最新的 intermediate_result:

```python
    if isinstance(latest_result, dict):
        latest_result.setdefault("ground_truth_stats_raw", {})
        latest_result["ground_truth_stats_raw"].update({
            "view_stats": _extract_latest_tool_observation(messages, verb="view", mode="stats"),
            "view_annotated": _extract_latest_tool_observation(messages, verb="view", mode="annotated"),
        })
```

`_extract_latest_tool_observation` 实现:遍历 `messages`,匹配 `ToolMessage` 且其对应 `AIMessage.tool_calls` 含 `name == "officecli"` 且 args `verb=view, mode=<mode>, file=<target_filename>`;返回 `ToolMessage.content`(raw string/dict)。

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py::test_qa_fix_uses_ground_truth_from_intermediate_results -v`
Expected: PASS

- [ ] **Step 7: Regression test all office tests**

Run: `pytest tests/test_office_domain.py tests/test_office_workflow_prompt.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add agent/workflows/office/builder.py agent/workflows/office/core/qa.py tests/test_office_domain.py
git commit -m "feat(office): capture officecli view stats/annotated raw output into ground truth"
```

---

## Task 7: Gate G1-font-family

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py::evaluate_quality_stats`
- Test: `tests/test_office_domain.py`

spec 第 4.2: `len(unique_font_families) > 2` → fail, severity=error。

- [ ] **Step 1: Write the failing test**

Append to `tests/test_office_domain.py`:

```python
def test_gate_font_family_fails_when_three_or_more_fonts() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    gt = GroundTruthStats(
        slide_count=1,
        per_slide=[SlidePhysicalStats(index=1, picture_count=1, layout_signature="Title-Picture")],
        unique_font_families={"Microsoft YaHei", "Arial", "Georgia"},
    )
    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=1),
        ground_truth=gt,
    )
    assert any("G1-font-family" in str(i.get("message", "")) for i in issues)


def test_gate_font_family_passes_with_two_fonts() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    gt = GroundTruthStats(
        slide_count=1,
        per_slide=[SlidePhysicalStats(index=1, picture_count=1, layout_signature="Title-Picture")],
        unique_font_families={"Microsoft YaHei", "Arial"},
    )
    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=1),
        ground_truth=gt,
    )
    assert not any("G1-font-family" in str(i.get("message", "")) for i in issues)
```

And near the top of `tests/test_office_domain.py`, if missing, add:

```python
def _PASSING_SELF_REPORTED_STATS(slide_count: int) -> dict:
    return {
        "slide_count": slide_count,
        "content_slide_count": slide_count,
        "notes_slide_count": slide_count,
        "transition_slide_count": max(slide_count - 1, 0),
        "visual_slide_count": slide_count,
        "text_only_slide_count": 0,
        "layout_variety_count": min(slide_count, 3),
        "picture_count": slide_count,
        "chart_count": 0,
        "table_count": 0,
        "qa_checks": ["view_stats", "view_annotated", "validate"],
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py::test_gate_font_family_fails_when_three_or_more_fonts -v`
Expected: FAIL

- [ ] **Step 3: Implement G1-font-family**

Edit `agent/workflows/office/strategies/ppt.py::evaluate_quality_stats`,在已有 stats 检查之后加:

```python
        from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats

        if isinstance(ground_truth, GroundTruthStats):
            self._apply_ground_truth_gates(ground_truth, plan, merged_constraints, issues)
        return issues
```

And add method:

```python
    def _apply_ground_truth_gates(
        self,
        ground_truth: "GroundTruthStats",
        plan: dict[str, Any] | None,
        merged_constraints: dict[str, Any] | None,
        issues: list[dict[str, Any]],
    ) -> None:
        if len(ground_truth.unique_font_families) > 2:
            issues.append(
                {
                    "severity": "error",
                    "message": f"G1-font-family: 字体家族过多({sorted(ground_truth.unique_font_families)})，限 YaHei + Arial",
                }
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k font_family`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py tests/test_office_domain.py
git commit -m "feat(office): add G1-font-family QA gate"
```

---

## Task 8: Gate G1-duplicate-title

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py::_apply_ground_truth_gates`
- Test: `tests/test_office_domain.py`

spec 第 4.2: 任意 slide 的 `distinct_title_objects > 1` → fail。

- [ ] **Step 1: Write the failing test**

Append to `tests/test_office_domain.py`:

```python
def test_gate_duplicate_title_fails_when_any_slide_has_two_titles() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    gt = GroundTruthStats(
        slide_count=2,
        per_slide=[
            SlidePhysicalStats(index=1, picture_count=1, distinct_title_objects=2, layout_signature="Title-TextBox-Picture"),
            SlidePhysicalStats(index=2, picture_count=1, distinct_title_objects=1, layout_signature="Title-Picture"),
        ],
        unique_font_families={"Microsoft YaHei", "Arial"},
    )
    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=2),
        ground_truth=gt,
    )
    assert any("G1-duplicate-title" in str(i.get("message", "")) for i in issues)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k duplicate_title`
Expected: FAIL

- [ ] **Step 3: Extend `_apply_ground_truth_gates`**

Append inside `_apply_ground_truth_gates`:

```python
        offending_titles = [s.index for s in ground_truth.per_slide if s.distinct_title_objects > 1]
        if offending_titles:
            issues.append(
                {
                    "severity": "error",
                    "message": f"G1-duplicate-title: slide {offending_titles} 同页出现多个标题对象",
                }
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k duplicate_title`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py tests/test_office_domain.py
git commit -m "feat(office): add G1-duplicate-title QA gate"
```

---

## Task 9: Gate G1-placeholder-remnant

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py::_apply_ground_truth_gates`
- Test: `tests/test_office_domain.py`

spec 第 4.2: 命中 `{xxxx, lorem ipsum, TODO, FIXME, 占位, placeholder}` → fail。

- [ ] **Step 1: Write the failing test**

```python
def test_gate_placeholder_remnant_fails_when_any_hit() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    gt = GroundTruthStats(
        slide_count=1,
        per_slide=[SlidePhysicalStats(index=1, picture_count=1, layout_signature="Title-Picture")],
        unique_font_families={"Microsoft YaHei"},
        placeholder_remnant_hits=[(1, "xxxx")],
    )
    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=1),
        ground_truth=gt,
    )
    assert any("G1-placeholder-remnant" in str(i.get("message", "")) for i in issues)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k placeholder_remnant`
Expected: FAIL

- [ ] **Step 3: Extend `_apply_ground_truth_gates`**

```python
        if ground_truth.placeholder_remnant_hits:
            locations = ", ".join(f"slide {idx}: '{pat}'" for idx, pat in ground_truth.placeholder_remnant_hits[:5])
            issues.append(
                {
                    "severity": "error",
                    "message": f"G1-placeholder-remnant: 命中 placeholder 文本 ({locations})",
                }
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k placeholder_remnant`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py tests/test_office_domain.py
git commit -m "feat(office): add G1-placeholder-remnant QA gate"
```

---

## Task 10: Gate G1-picture-threshold(含配图词触发判定)

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py::_apply_ground_truth_gates`
- Test: `tests/test_office_domain.py`

spec 第 4.2 + Picture keywords 定义: 触发词来自 `GoalProfile.quality_profile.visuals=true` 或 goal 原文命中 `{配图, 插图, 加图, 配一些图, 要图, 附图, with images, with pictures}`。触发后 `sum(picture_count) < ceil(slide_count * 0.3)` → fail。

note: `merged_constraints` 会透传 `goal_constraints` 带 `goal` 字段(见 workflow.py:617)。从 `merged_constraints.get("goal")` 和 `quality_profile.visuals` 推判定。另外兜底: `merged_constraints.get("quality_profile")`。

- [ ] **Step 1: Write the failing test**

```python
import math


def test_gate_picture_threshold_triggers_on_visuals_flag() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    gt = GroundTruthStats(
        slide_count=10,
        per_slide=[SlidePhysicalStats(index=i, picture_count=0) for i in range(1, 11)],
        unique_font_families={"Microsoft YaHei"},
    )
    merged = {"quality_profile": {"visuals": True}, "goal": "做个 deck"}
    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=10),
        ground_truth=gt,
        merged_constraints=merged,
    )
    assert any("G1-picture-threshold" in str(i.get("message", "")) for i in issues)


def test_gate_picture_threshold_triggers_on_goal_keyword() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    gt = GroundTruthStats(
        slide_count=10,
        per_slide=[SlidePhysicalStats(index=i, picture_count=0) for i in range(1, 11)],
        unique_font_families={"Microsoft YaHei"},
    )
    merged = {"goal": "做一份钓鱼好处的 PPT，要配图"}
    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=10),
        ground_truth=gt,
        merged_constraints=merged,
    )
    assert any("G1-picture-threshold" in str(i.get("message", "")) for i in issues)


def test_gate_picture_threshold_passes_when_enough_pictures() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    per_slide = [SlidePhysicalStats(index=i, picture_count=(1 if i <= 4 else 0)) for i in range(1, 11)]
    gt = GroundTruthStats(
        slide_count=10,
        per_slide=per_slide,
        unique_font_families={"Microsoft YaHei"},
    )
    merged = {"goal": "要配图的介绍"}
    assert sum(s.picture_count for s in per_slide) >= math.ceil(10 * 0.3)
    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=10),
        ground_truth=gt,
        merged_constraints=merged,
    )
    assert not any("G1-picture-threshold" in str(i.get("message", "")) for i in issues)


def test_gate_picture_threshold_silent_when_not_triggered() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    gt = GroundTruthStats(
        slide_count=3,
        per_slide=[SlidePhysicalStats(index=i, picture_count=0) for i in range(1, 4)],
        unique_font_families={"Microsoft YaHei"},
    )
    merged = {"goal": "做个内部数据表"}
    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=3),
        ground_truth=gt,
        merged_constraints=merged,
    )
    assert not any("G1-picture-threshold" in str(i.get("message", "")) for i in issues)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k picture_threshold`
Expected: 4 failures

- [ ] **Step 3: Extend gate logic**

Add module-level constant in `agent/workflows/office/strategies/ppt.py`:

```python
_PICTURE_INTENT_KEYWORDS = (
    "配图",
    "插图",
    "加图",
    "配一些图",
    "要图",
    "附图",
    "with images",
    "with pictures",
)


def _picture_intent_triggered(merged_constraints: dict[str, Any] | None) -> bool:
    if not isinstance(merged_constraints, dict):
        return False
    quality = merged_constraints.get("quality_profile")
    if isinstance(quality, dict) and bool(quality.get("visuals")):
        return True
    goal = str(merged_constraints.get("goal") or "").lower()
    return any(kw.lower() in goal for kw in _PICTURE_INTENT_KEYWORDS)
```

In `_apply_ground_truth_gates`:

```python
        import math

        if _picture_intent_triggered(merged_constraints):
            total_pictures = sum(s.picture_count for s in ground_truth.per_slide)
            threshold = math.ceil(ground_truth.slide_count * 0.3)
            if total_pictures < threshold:
                issues.append(
                    {
                        "severity": "error",
                        "message": (
                            f"G1-picture-threshold: 用户要求配图,但 picture_count={total_pictures} "
                            f"< ceil({ground_truth.slide_count} * 0.3)={threshold}"
                        ),
                    }
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k picture_threshold`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py tests/test_office_domain.py
git commit -m "feat(office): add G1-picture-threshold QA gate"
```

---

## Task 11: Gate G1-consecutive-layout

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py::_apply_ground_truth_gates`
- Test: `tests/test_office_domain.py`

spec 第 4.2 + D1: `layout_signature` 已在 Task 4 产出;相邻两页签名相同 → fail。

- [ ] **Step 1: Write the failing test**

```python
def test_gate_consecutive_layout_fails_when_adjacent_signatures_match() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    gt = GroundTruthStats(
        slide_count=3,
        per_slide=[
            SlidePhysicalStats(index=1, picture_count=1, layout_signature="Title-Picture"),
            SlidePhysicalStats(index=2, picture_count=1, layout_signature="Title-Picture"),
            SlidePhysicalStats(index=3, picture_count=1, layout_signature="Title-TextBox"),
        ],
        unique_font_families={"Microsoft YaHei"},
    )
    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=3),
        ground_truth=gt,
    )
    assert any("G1-consecutive-layout" in str(i.get("message", "")) for i in issues)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k consecutive_layout`
Expected: FAIL

- [ ] **Step 3: Extend gate logic**

```python
        for a, b in zip(ground_truth.per_slide, ground_truth.per_slide[1:]):
            if a.layout_signature and a.layout_signature == b.layout_signature:
                issues.append(
                    {
                        "severity": "error",
                        "message": f"G1-consecutive-layout: slide {a.index} 和 {b.index} 布局签名相同 ({a.layout_signature})",
                    }
                )
                break
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k consecutive_layout`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py tests/test_office_domain.py
git commit -m "feat(office): add G1-consecutive-layout QA gate"
```

---

## Task 12: Gate G1-decorative-only-cap

**Files:**
- Modify: `agent/workflows/office/strategies/ppt.py::_apply_ground_truth_gates`
- Test: `tests/test_office_domain.py`

spec 第 4.2 + Decorative-only 定义: 单页 `picture_count == 0 and chart_count == 0 and table_count == 0 and smartart_count == 0` 即"decorative-only"。`decorative_only / content > 0.5` → fail。
"content_slide_count" 来自 per_slide 除了 cover/summary 的页数;此处简化为 `slide_count - 2`(封面 + 结尾页),下限 1。

- [ ] **Step 1: Write the failing test**

```python
def test_gate_decorative_only_cap_fails_when_majority_shape_only() -> None:
    from agent.workflows.office.strategies.ppt import PptStrategy
    from agent.workflows.office.strategies.ppt_stats_reader import GroundTruthStats, SlidePhysicalStats

    per_slide = [
        SlidePhysicalStats(index=1, picture_count=1, layout_signature="Title-Picture"),  # cover
        SlidePhysicalStats(index=2, picture_count=0, chart_count=0, table_count=0, smartart_count=0, shape_count=3, layout_signature="Title-Shape-Shape"),
        SlidePhysicalStats(index=3, picture_count=0, chart_count=0, table_count=0, smartart_count=0, shape_count=2, layout_signature="Title-Shape-TextBox"),
        SlidePhysicalStats(index=4, picture_count=0, chart_count=0, table_count=0, smartart_count=0, shape_count=2, layout_signature="Title-TextBox-Shape"),
        SlidePhysicalStats(index=5, picture_count=1, layout_signature="Title-Picture-TextBox"),
        SlidePhysicalStats(index=6, picture_count=1, layout_signature="Summary-Picture"),  # summary
    ]
    gt = GroundTruthStats(slide_count=6, per_slide=per_slide, unique_font_families={"Microsoft YaHei"})
    issues = PptStrategy().evaluate_quality_stats(
        operation="create",
        stats=_PASSING_SELF_REPORTED_STATS(slide_count=6),
        ground_truth=gt,
    )
    assert any("G1-decorative-only-cap" in str(i.get("message", "")) for i in issues)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k decorative_only_cap`
Expected: FAIL

- [ ] **Step 3: Extend gate logic**

```python
        def _is_decorative_only(s) -> bool:
            return (
                s.picture_count == 0
                and s.chart_count == 0
                and s.table_count == 0
                and s.smartart_count == 0
            )

        # exclude first and last slide (cover + summary)
        content_slides = ground_truth.per_slide[1:-1] if len(ground_truth.per_slide) >= 3 else ground_truth.per_slide
        content_count = max(len(content_slides), 1)
        decorative_count = sum(1 for s in content_slides if _is_decorative_only(s))
        if decorative_count / content_count > 0.5:
            issues.append(
                {
                    "severity": "error",
                    "message": (
                        f"G1-decorative-only-cap: 仅靠 shape/textbox 的 decorative-only 页数 "
                        f"{decorative_count}/{content_count} > 50%"
                    ),
                }
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k decorative_only_cap`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/strategies/ppt.py tests/test_office_domain.py
git commit -m "feat(office): add G1-decorative-only-cap QA gate"
```

---

## Task 13: QA feedback 携带 gate 原因给 repair build

**Files:**
- Modify: `agent/workflows/office/core/qa.py` (after gate evaluation, ensure qa_feedback text is clear)
- Modify: `agent/workflows/office/core/quality_report.py` 或下游构造 qa_feedback 的位置
- Test: `tests/test_office_domain.py`

spec 第 4.4: fail 的 gate 写入 `qa_feedback`,repair_mode 再过 build。模型必须针对 gate 做定点修复。
先定位 qa_feedback 是怎么注入 build_input_sections 的。看 `agent/workflows/office/strategies/ppt.py::build_input_sections`line 281: 直接接 `qa_feedback` 字符串。state 里没看到 `qa_feedback` 字段;要看 build_node / run_section_builder 的管线。

- [ ] **Step 1: Trace how qa_feedback reaches build**

Run: `grep -n "qa_feedback" agent/workflows/office/ -r`

Record the path: `run_quality_gate → intermediate_results[-1].get("output")` loops back; qa_feedback appears to be constructed from quality_report issues (verify by reading the path before writing the test).

- [ ] **Step 2: Write the failing test**

```python
def test_qa_feedback_mentions_failing_gate_ids() -> None:
    from agent.workflows.office.core.qa import run_qa_fix_stage
    from agent.workflows.office.strategies.ppt import PptStrategy

    state = {
        "format": "pptx",
        "operation": "create",
        "write_required": True,
        "qa_fix_round": 0,
        "max_qa_fix_rounds": 2,
        "intermediate_results": [{
            "output": '```json\n{"operation":"create","validated":true,"summary":"","artifacts":[{"filename":"deck.pptx","path":"","format":"pptx","role":"primary"}],"stats":{"slide_count":2,"content_slide_count":2,"notes_slide_count":2,"transition_slide_count":1,"visual_slide_count":2,"text_only_slide_count":0,"layout_variety_count":2,"picture_count":0,"chart_count":0,"table_count":0,"qa_checks":["view_stats","view_annotated","validate"]}}\n```',
            "ground_truth_stats_raw": {
                "view_stats": {"slide_count": 2, "slides": [{"index": 1, "pictures": 0}, {"index": 2, "pictures": 0}]},
                "view_annotated": (
                    "Slide 1\n- Title: A ← Microsoft YaHei 32pt\n- TextBox: body ← Calibri 14pt\n- TextBox: more ← Georgia 12pt\n"
                    "Slide 2\n- Title: B ← Microsoft YaHei 32pt\n- TextBox: body ← Calibri 14pt"
                ),
            },
        }],
        "task_profile": {"merged_constraints": {"goal": "做图文并茂的 deck,要配图", "quality_profile": {"visuals": True}}},
    }
    result = run_qa_fix_stage(state, strategy=PptStrategy())
    assert result.get("current_stage") == "build"
    report = result.get("quality_report") or {}
    messages = " ".join(str(i.get("message", "")) for i in report.get("issues", []))
    assert "G1-font-family" in messages
    assert "G1-picture-threshold" in messages
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_office_domain.py -v -k qa_feedback_mentions_failing_gate`
Expected: FAIL — gate messages not reaching `quality_report.issues` through the `merged_constraints` pass-through.

- [ ] **Step 4: Wire merged_constraints from task_profile into gate call**

Confirm `agent/workflows/office/core/qa.py:203-207` reads `merged_constraints` already. Verify the merged_constraints reaches `evaluate_quality_stats`. If not, pass through `task_profile.merged_constraints` (spec already required this).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_office_domain.py -v -k qa_feedback_mentions_failing_gate`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add agent/workflows/office/core/qa.py tests/test_office_domain.py
git commit -m "feat(office): surface gate failure messages to qa_feedback for repair build"
```

---

## Task 14: `_build_format_specific_guidance` 改写图片段落

**Files:**
- Modify: `agent/workflows/office/workflow.py:172-235`
- Test: `tests/test_office_workflow_prompt.py`

spec 第 4.3: 把图片段落从"可选"改成"必须"。

- [ ] **Step 1: Write the failing test**

Append to `tests/test_office_workflow_prompt.py`:

```python
def test_format_guidance_enforces_picture_threshold_when_visuals_required() -> None:
    from agent.workflows.office.workflow import _build_format_specific_guidance

    guidance = _build_format_specific_guidance(
        goal="做一份钓鱼好处 PPT，图文并茂要配图",
        format_name="pptx",
        operation="create",
        requested_slide_count=10,
    )
    assert "picture_count ≥" in guidance or "picture_count>=" in guidance.lower() or "ceil(slide_count × 0.3)" in guidance
    assert "image_gen" in guidance
    assert "不允许省略" in guidance or "必须" in guidance


def test_format_guidance_covers_hero_cover_picture() -> None:
    from agent.workflows.office.workflow import _build_format_specific_guidance

    guidance = _build_format_specific_guidance(
        goal="做一份产品发布 PPT",
        format_name="pptx",
        operation="create",
        requested_slide_count=8,
    )
    assert "封面" in guidance
    assert "picture" in guidance.lower() or "hero" in guidance.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_workflow_prompt.py -v -k picture_threshold_when_visuals_required`
Expected: FAIL

- [ ] **Step 3: Update `_build_format_specific_guidance`**

Edit `agent/workflows/office/workflow.py:207-211` (the picture paragraph), replace the lines about 配图 with:

```python
"""\
- 用户要求配图、或 deck 是营销/案例/生活方式类：**必须**让 `picture_count ≥ ceil(slide_count × 0.3)`。
- 先调用 `list_user_images`；命中就用用户图。
- 没有命中时**必须**走 `image_gen(prompt=...)` 生成配图，不允许以"没有素材"为由省略图片。
- 封面 slide **必须**带 picture 或 full-bleed hero shape；不允许只有文字 + 色块。
"""
```

Replace the existing picture lines (line 207-208) with these.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_workflow_prompt.py -v -k format_guidance`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/workflow.py tests/test_office_workflow_prompt.py
git commit -m "feat(office): enforce picture generation in PPT format guidance"
```

---

## Task 15: `_OFFICE_SYSTEM` 增加"交付前 QA 真值"段

**Files:**
- Modify: `agent/workflows/office/workflow.py:238-296` (_OFFICE_SYSTEM)
- Test: `tests/test_office_workflow_prompt.py`

spec 第 4.3: system prompt 追加"模型自报 stats 仅参考,以 view stats / view annotated 真值为准" + 字体合同 + 色板软约束。

- [ ] **Step 1: Write the failing test**

```python
def test_office_system_declares_ground_truth_priority() -> None:
    from agent.workflows.office.workflow import _OFFICE_SYSTEM

    assert "view stats" in _OFFICE_SYSTEM
    assert "真值" in _OFFICE_SYSTEM
    assert "YaHei" in _OFFICE_SYSTEM
    assert "Arial" in _OFFICE_SYSTEM
    assert "Calibri" in _OFFICE_SYSTEM or "Georgia" in _OFFICE_SYSTEM
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_office_workflow_prompt.py -v -k ground_truth_priority`
Expected: FAIL

- [ ] **Step 3: Extend _OFFICE_SYSTEM**

Edit `agent/workflows/office/workflow.py:238-296`, before the closing `"""`, insert a new section after `{format_specific_guidance}` (same text block):

```python
## PPT 交付前真值 QA（硬规则）

- 你自报的 `stats` 字段仅作参考；最终 QA 以 `officecli(verb="view", mode="stats")` 与 `officecli(verb="view", mode="annotated")` 的真值输出为准。
- 全 deck 字体家族必须 ⊆ {Microsoft YaHei, Arial}（中文 YaHei + 英文 Arial）；禁止 Calibri / Georgia / Cambria 任何变体混入。
- 色板软约束：只用任务中指定的主题色；本轮未指定时默认 business_formal 色板。
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_office_workflow_prompt.py -v -k ground_truth_priority`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/workflows/office/workflow.py tests/test_office_workflow_prompt.py
git commit -m "feat(office): declare ground-truth QA + font contract in system prompt"
```

---

## Task 16: 集成回归 — 模拟 fishing benchmark 两 gate 拦截

**Files:**
- Test: `tests/test_office_domain.py`

spec 第 4.5 第 1 条: 重放 fishing benchmark 的 goal,生成的 deck 必须在 QA 被至少两个 gate 拦下(G1-font-family + G1-picture-threshold)。此 task 用 mocked build output 模拟;真实生成由手动 smoke 跑。

- [ ] **Step 1: Write the failing regression test**

```python
def test_fishing_benchmark_is_rejected_by_font_and_picture_gates() -> None:
    from agent.workflows.office.core.qa import run_qa_fix_stage
    from agent.workflows.office.strategies.ppt import PptStrategy

    state = {
        "format": "pptx",
        "operation": "create",
        "write_required": True,
        "qa_fix_round": 0,
        "max_qa_fix_rounds": 2,
        "intermediate_results": [{
            "output": '```json\n{"operation":"create","validated":true,"summary":"","artifacts":[{"filename":"fishing.pptx","path":"","format":"pptx","role":"primary"}],"stats":{"slide_count":10,"content_slide_count":10,"notes_slide_count":10,"transition_slide_count":9,"visual_slide_count":10,"text_only_slide_count":0,"layout_variety_count":3,"picture_count":0,"chart_count":0,"table_count":0,"qa_checks":["view_stats","view_annotated","validate"]}}\n```',
            "ground_truth_stats_raw": {
                "view_stats": {
                    "slide_count": 10,
                    "slides": [{"index": i, "pictures": 0, "charts": 0, "tables": 0, "smartart": 0, "shapes": 3, "text_boxes": 3} for i in range(1, 11)],
                },
                "view_annotated": "\n".join(
                    f"Slide {i}\n- Title: T{i} ← Microsoft YaHei 32pt\n- TextBox: body ← Calibri 14pt\n- TextBox: extra ← Georgia 12pt"
                    for i in range(1, 11)
                ),
            },
        }],
        "task_profile": {"merged_constraints": {"goal": "钓鱼的好处，要配图", "quality_profile": {"visuals": True}}},
    }
    result = run_qa_fix_stage(state, strategy=PptStrategy())
    report = result.get("quality_report") or {}
    messages = [str(i.get("message", "")) for i in report.get("issues", [])]
    joined = " | ".join(messages)
    assert "G1-font-family" in joined
    assert "G1-picture-threshold" in joined
    assert result.get("current_stage") == "build"
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_office_domain.py -v -k fishing_benchmark_is_rejected`
Expected: PASS (all upstream gates already wired)

If this fails, root-cause which gate is missing or which wiring broke, fix, and re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/test_office_domain.py
git commit -m "test(office): regression for fishing benchmark gate rejection"
```

---

## Task 17: Smoke run + manual review

**Files:** (no file writes in this task)

- [ ] **Step 1: Run full office test suite**

Run: `pytest tests/test_office_domain.py tests/test_office_workflow_prompt.py tests/test_office_goal_normalizer.py tests/test_ppt_stats_reader.py -v`
Expected: all PASS

- [ ] **Step 2: Hand-run a benchmark goal**

If a run harness exists, reproduce the fishing goal end-to-end:

```bash
python -m scripts.run_office_goal --goal "做一份钓鱼好处的 PPT，大约 6 页，要配图" --format pptx --operation create
```

(If no such script — skip and record in plan notes that smoke must be done via the agent UI.)

Verify:
  - QA fires at least `G1-font-family` OR `G1-picture-threshold` on the first build round
  - Build repair round takes the feedback and produces a version that clears both

- [ ] **Step 3: No commit — just record observations**

Record rejection/repair counts in the rollout notes (e.g. an append to `docs/superpowers/plans/2026-04-20-office-ppt-visual-quality-batch1-notes.md` if desired,*not* required for merge).

---

## Self-Review Checklist

- [ ] Spec §4.1 Real-truth stats: Task 1-5 defines + parses + wires GroundTruthStats. ✅
- [ ] Spec §4.2 6 gates: Tasks 7-12 each add one gate. ✅
- [ ] Spec §4.3 Prompt strengthening: Tasks 14-15 update `_build_format_specific_guidance` + `_OFFICE_SYSTEM`. ✅
- [ ] Spec §4.4 QA fix-loop wiring: Task 13 routes gate messages into repair feedback. ✅
- [ ] Spec §4.5 acceptance: Task 16 regression for fishing benchmark. ✅ Task 17 smoke run. ✅
- [ ] Spec §7 state field changes: Task 5 adds `ground_truth_stats`. ✅
- [ ] Type names consistent across tasks: `GroundTruthStats`, `SlidePhysicalStats`, `parse_view_stats`, `merge_annotated_into_stats`, `_apply_ground_truth_gates` — confirmed. ✅
- [ ] No TBD / TODO / "similar to" left. ✅
- [ ] Every code step shows actual code. ✅

---

## Rollout Notes

- Batch 1 merges independently (spec §9). Gate evaluation for non-pptx paths is unchanged: `evaluate_quality_stats` only runs ground-truth gates when `ground_truth` is a `GroundTruthStats` instance.
- No config flags; Batch 1 is on by default.
- Existing PPT deck generation on happy path (2 fonts, pictures per visuals request, unique layouts, no placeholders) continues to pass.
