from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.domains.office.strategies.default import DefaultOfficeStrategy

_KNOWN_SHEET_LABELS = {
    "rawdata",
    "raw_data",
    "raw-data",
    "summary",
    "dashboard",
    "budget",
    "data",
    "metrics",
    "kpi",
    "overview",
    "details",
    "detail",
    "report",
    "analysis",
    "forecast",
    "assumptions",
    "inputs",
    "output",
    "outputs",
}
_SHEET_NAME_REJECT_TOKENS = (
    "rename ",
    "preserve ",
    "update ",
    "change ",
    "keep ",
    "include ",
    "contains ",
    "should ",
    "must ",
    "need ",
    "formula",
    "column",
    "header",
    "row ",
)
_EXCEL_FORBIDDEN_SHEET_CHARS = set("[]:*?/\\")


class XlsxStrategy(DefaultOfficeStrategy):
    def build_plan(
        self,
        *,
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        title = _infer_workbook_title(goal, default_create_file)
        sheets = _build_sheets(goal=goal, merged_constraints=merged_constraints)
        batches = _build_batches(sheets=sheets, build_batch_size=build_batch_size)
        sheet_count = len(sheets)
        return {
            "title": title,
            "sheet_count": sheet_count,
            "sheets": sheets,
            "batches": batches,
            # Keep the shared workflow stable until workbook-native naming is threaded through it.
            "slide_count": sheet_count,
            "slides": sheets,
        }

    def summarize_plan(self, plan: dict[str, Any]) -> str:
        if not isinstance(plan, dict):
            return ""
        sheets = list(plan.get("sheets") or [])
        batches = list(plan.get("batches") or [])
        lines = [
            f"- workbook_title: {str(plan.get('title', '') or '').strip()}",
            f"- planned_sheet_count: {int(plan.get('sheet_count', 0) or 0)}",
        ]
        if sheets:
            lines.append("- sheet_outline:")
            for index, sheet in enumerate(sheets[:12], start=1):
                lines.append(
                    f"  - sheet[{index}] {str(sheet.get('name', '') or '').strip()} ({str(sheet.get('sheet_type', '') or '').strip()}) :: {str(sheet.get('purpose', '') or '').strip()}"
                )
        if batches:
            lines.append("- build_batches:")
            for batch in batches:
                lines.append(
                    f"  - batch[{int(batch.get('index', 0) or 0)}] sheets {int(batch.get('sheet_start', 0) or 0)}-{int(batch.get('sheet_end', 0) or 0)}: {', '.join(str(item) for item in batch.get('sheet_names', []) or [])}"
                )
        return "\n".join(lines)

    def validate_plan(
        self,
        *,
        plan: dict[str, Any],
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(plan, dict):
            fallback = self.build_plan(
                goal=goal,
                requested_slide_count=requested_slide_count,
                build_batch_size=build_batch_size,
                default_create_file=default_create_file,
                merged_constraints=merged_constraints,
            )
            return fallback, ["plan_not_dict"]

        issues: list[str] = []
        title = str(plan.get("title", "") or "").strip()
        if not title:
            issues.append("missing_title")

        raw_sheets = plan.get("sheets")
        if not isinstance(raw_sheets, list) or not raw_sheets:
            issues.append("missing_sheets")
            raw_sheets = []

        normalized_sheets: list[dict[str, Any]] = []
        seen_sheet_names: set[str] = set()
        sheet_shape_invalid = False
        used_structural_ids: dict[str, int] = {}
        for sheet in raw_sheets:
            if not isinstance(sheet, dict):
                continue
            preserved_table_regions = _coerce_list_field(sheet, "table_regions", fallback=[])
            _seed_structural_ids_from_regions(preserved_table_regions, used_structural_ids=used_structural_ids)
        for sheet in raw_sheets:
            if not isinstance(sheet, dict):
                issues.append("invalid_sheet_entry")
                sheet_shape_invalid = True
                continue
            name = str(sheet.get("name", "") or "").strip()
            if not _is_legal_sheet_name(name):
                issues.append("invalid_sheet_name")
                sheet_shape_invalid = True
                continue
            name_key = _sheet_name_key(name)
            if name_key in seen_sheet_names:
                issues.append("duplicate_sheet_name")
                sheet_shape_invalid = True
                continue
            seen_sheet_names.add(name_key)
            has_explicit_table_regions = "table_regions" in sheet
            preserved_table_regions = _coerce_list_field(sheet, "table_regions", fallback=[])
            normalized_sheets.append(
                {
                    "name": name,
                    "purpose": str(sheet.get("purpose", "") or "").strip() or _sheet_purpose(name),
                    "sheet_type": str(sheet.get("sheet_type", "") or "").strip() or _sheet_type(name),
                    "columns": _coerce_list_field(sheet, "columns", fallback=_sheet_columns(name)),
                    "table_regions": (
                        preserved_table_regions
                        if has_explicit_table_regions
                        else _table_regions(name, used_structural_ids=used_structural_ids)
                    ),
                    "formula_regions": _coerce_list_field(sheet, "formula_regions", fallback=_formula_regions(name)),
                    "chart_regions": _coerce_list_field(sheet, "chart_regions", fallback=_chart_regions(name)),
                    "validation_rules": _coerce_list_field(sheet, "validation_rules", fallback=_validation_rules(name)),
                }
            )

        if sheet_shape_invalid or not normalized_sheets:
            normalized = self.build_plan(
                goal=goal,
                requested_slide_count=max(_coerce_int(plan.get("sheet_count"), default=0), requested_slide_count),
                build_batch_size=build_batch_size,
                default_create_file=default_create_file,
                merged_constraints=merged_constraints,
            )
            if title:
                normalized["title"] = title
            return normalized, issues

        raw_batches = plan.get("batches")
        normalized_batches = _normalize_batches(raw_batches, normalized_sheets, build_batch_size=build_batch_size)
        if not isinstance(raw_batches, list) or not raw_batches:
            issues.append("missing_batches")

        normalized_title = title or _infer_workbook_title(goal, default_create_file)
        normalized_plan = {
            "title": normalized_title,
            "sheet_count": len(normalized_sheets),
            "sheets": normalized_sheets,
            "batches": normalized_batches,
            "slide_count": len(normalized_sheets),
            "slides": normalized_sheets,
        }
        return normalized_plan, issues

    def get_current_batch(self, plan: dict[str, Any], batch_index: int) -> dict[str, Any] | None:
        batches = list(plan.get("batches") or []) if isinstance(plan, dict) else []
        if batch_index < 0 or batch_index >= len(batches):
            return None
        return dict(batches[batch_index])

    def build_phase_guidance(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        qa_feedback: str,
    ) -> str:
        batch = self.get_current_batch(plan, current_batch_index)
        if repair_mode:
            guidance = [
                "- 当前阶段: repair run",
                "- 本轮只允许修复上一轮 QA 指出的工作簿问题。",
                "- 修复完成后，执行 validate / view stats，并返回更新后的 stats。",
            ]
            if qa_feedback:
                guidance.extend(["- 必须优先修复这些问题：", qa_feedback])
            return "\n".join(guidance)

        if batch is None:
            return "- 当前阶段: build\n- 所有 sheet batch 已规划完成，本轮如果继续，只允许执行最终 QA。"

        lines = [
            "- 当前阶段: build",
            f"- 当前 batch: {current_batch_index + 1}/{max(len(plan.get('batches', []) or []), 1)}",
            f"- 只处理 sheet {int(batch.get('sheet_start', 0) or 0)}-{int(batch.get('sheet_end', 0) or 0)}。",
        ]
        sheet_names = ", ".join(str(item) for item in batch.get("sheet_names", []) or [])
        if sheet_names:
            lines.append(f"- 本批 sheet: {sheet_names}")
        lines.append("- 先保证工作簿结构、表区域和公式区域完整，再做最终交付。")
        return "\n".join(lines)

    def build_input_sections(
        self,
        *,
        goal: str,
        operation: str,
        format_hint: str,
        runtime_target: str,
        default_create_file: str,
        requested_slide_count: int | None,
        build_batch_size: int,
        source_files: list[str],
        context: str,
        qa_feedback: str,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
    ) -> list[str]:
        sections = [
            goal,
            "",
            "执行上下文：",
            f"- operation: {operation}",
            f"- format: {format_hint}",
            f"- runtime_target: {runtime_target}",
        ]
        if default_create_file:
            sections.append(f"- default_create_file: {default_create_file}")
        sections.append(f"- build_batch_size: {build_batch_size}")
        if plan:
            sections.extend(["- workbook_plan:", self.summarize_plan(plan)])
        sections.append(f"- current_batch_index: {current_batch_index}")
        sections.append(f"- repair_mode: {str(repair_mode).lower()}")
        batch = self.get_current_batch(plan, current_batch_index)
        if batch is not None:
            sections.append(
                f"- current_batch_sheet_range: {int(batch.get('sheet_start', 0) or 0)}-{int(batch.get('sheet_end', 0) or 0)}"
            )
        if source_files:
            sections.append("- source_files:")
            sections.extend(f"  - {item}" for item in source_files)
        if context:
            sections.extend(["", "已有上下文：", context])
        if qa_feedback:
            sections.extend(["", "上轮 QA 未通过，必须先修正这些问题：", qa_feedback])
        return sections

    def evaluate_quality_stats(
        self,
        *,
        operation: str,
        stats: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if operation not in {"create", "edit", "transform"}:
            return []
        if not isinstance(stats, dict) or not stats:
            return [{"severity": "error", "message": "XLSX 写入结果缺少质量 stats"}]
        sheet_count = _coerce_int(stats.get("sheet_count"), default=0)
        if sheet_count <= 0:
            return [{"severity": "error", "message": "XLSX 质量 stats 缺少有效的 sheet_count"}]
        return []

    def advance_after_build(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        completed_pages: int,
    ) -> dict[str, Any]:
        if repair_mode:
            return {
                "current_batch_index": current_batch_index,
                "completed_pages": completed_pages,
                "next_stage": "qa_fix",
            }

        batch = self.get_current_batch(plan, current_batch_index)
        next_batch_index = current_batch_index + 1
        next_completed_pages = completed_pages
        if batch is not None:
            next_completed_pages = max(next_completed_pages, int(batch.get("sheet_end", 0) or 0))
        next_stage = "build" if next_batch_index < len(plan.get("batches", []) or []) else "qa_fix"
        return {
            "current_batch_index": next_batch_index,
            "completed_pages": next_completed_pages,
            "next_stage": next_stage,
        }


def _infer_workbook_title(goal: str, default_create_file: str) -> str:
    compact = str(goal or "").replace("\n", " ").strip()
    if compact:
        return compact[:80]
    stem = Path(str(default_create_file or "")).stem.replace("-", " ").strip()
    return stem or "Workbook"


def _build_sheets(*, goal: str, merged_constraints: dict[str, Any] | None) -> list[dict[str, Any]]:
    names = _derive_sheet_names(goal=goal, merged_constraints=merged_constraints)
    used_structural_ids: dict[str, int] = {}
    sheets: list[dict[str, Any]] = []
    for name in names:
        sheets.append(
            {
                "name": name,
                "purpose": _sheet_purpose(name),
                "sheet_type": _sheet_type(name),
                "columns": _sheet_columns(name),
                "table_regions": _table_regions(name, used_structural_ids=used_structural_ids),
                "formula_regions": _formula_regions(name),
                "chart_regions": _chart_regions(name),
                "validation_rules": _validation_rules(name),
            }
        )
    return sheets


def _derive_sheet_names(*, goal: str, merged_constraints: dict[str, Any] | None) -> list[str]:
    hard_requirements = []
    if isinstance(merged_constraints, dict):
        goal_constraints = merged_constraints.get("goal_constraints")
        if isinstance(goal_constraints, dict):
            raw_requirements = goal_constraints.get("hard_requirements")
            if isinstance(raw_requirements, (list, tuple)):
                hard_requirements = list(raw_requirements)

    names: list[str] = []
    seen_names: set[str] = set()
    for item in hard_requirements:
        name = str(item or "").strip()
        name_key = _sheet_name_key(name)
        if _is_legal_sheet_name(name) and _looks_like_sheet_name(name) and name_key not in seen_names:
            names.append(name)
            seen_names.add(name_key)

    if isinstance(merged_constraints, dict):
        structure_constraints = merged_constraints.get("reference_structure_constraints")
        units = structure_constraints.get("units") if isinstance(structure_constraints, dict) else []
        if isinstance(units, list):
            for unit in units:
                if not isinstance(unit, dict):
                    continue
                name = str(unit.get("name", "") or "").strip()
                name_key = _sheet_name_key(name)
                if _is_legal_sheet_name(name) and _looks_like_sheet_name(name) and name_key not in seen_names:
                    names.append(name)
                    seen_names.add(name_key)

    if names:
        return names

    fallback_names = []
    lowered = str(goal or "").lower()
    if "dashboard" in lowered or "仪表盘" in goal:
        fallback_names.append("Dashboard")
    if "summary" in lowered or "汇总" in goal or "总结" in goal:
        fallback_names.append("Summary")
    if "raw" in lowered or "原始" in goal:
        fallback_names.append("RawData")
    return fallback_names or ["Sheet1"]


def _looks_like_sheet_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if not _is_legal_sheet_name(text):
        return False
    lowered = text.lower()
    if any(token in lowered for token in _SHEET_NAME_REJECT_TOKENS):
        return False
    normalized = lowered.replace("-", " ").replace("_", " ").replace("(", " ").replace(")", " ").replace("/", " ")
    normalized = normalized.replace(",", " ").replace(".", " ").replace(";", " ").replace(":", " ")
    normalized = normalized.replace("?", " ").replace("!", " ")
    normalized = normalized.replace("。", " ").replace("；", " ").replace("：", " ").replace("？", " ").replace("！", " ")
    tokens = [part for part in normalized.split() if part]
    if not tokens or len(tokens) > 6:
        return False
    compact = lowered.replace(" ", "")
    if compact in _KNOWN_SHEET_LABELS:
        return True
    if any("\u4e00" <= char <= "\u9fff" for char in text):
        return len(text) <= 12
    if all(all(char.isalnum() for char in token) for token in tokens) and any(
        any(char.isalpha() for char in token) for token in tokens
    ):
        return True
    if len(tokens) == 1 and any(char.isalnum() for char in text) and text[:1].isupper():
        return True
    return False


def _is_legal_sheet_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if len(text) > 31:
        return False
    return not any(char in _EXCEL_FORBIDDEN_SHEET_CHARS for char in text)


def _sheet_name_key(value: str) -> str:
    return str(value or "").strip().lower()


def _sheet_type(name: str) -> str:
    lowered = str(name or "").strip().lower()
    if "dashboard" in lowered or "仪表盘" in name:
        return "dashboard"
    if "summary" in lowered or "汇总" in name or "总览" in name:
        return "summary"
    if "raw" in lowered or "data" in lowered or "明细" in name or "原始" in name:
        return "raw_data"
    return "worksheet"


def _sheet_purpose(name: str) -> str:
    sheet_type = _sheet_type(name)
    if sheet_type == "dashboard":
        return "Display KPI highlights and charts derived from summary metrics."
    if sheet_type == "summary":
        return "Aggregate the core metrics needed for review and downstream analysis."
    if sheet_type == "raw_data":
        return "Store source records in a structured table for calculations."
    return "Support workbook calculations and organization."


def _sheet_columns(name: str) -> list[dict[str, str]]:
    sheet_type = _sheet_type(name)
    if sheet_type == "dashboard":
        return [
            {"name": "Metric", "type": "text"},
            {"name": "Value", "type": "number"},
            {"name": "Trend", "type": "text"},
        ]
    if sheet_type == "summary":
        return [
            {"name": "Metric", "type": "text"},
            {"name": "Value", "type": "number"},
            {"name": "Variance", "type": "number"},
        ]
    return [
        {"name": "Category", "type": "text"},
        {"name": "Amount", "type": "number"},
        {"name": "Date", "type": "date"},
    ]


def _table_regions(name: str, *, used_structural_ids: dict[str, int]) -> list[dict[str, str]]:
    base_identifier = f"{_structural_identifier(name)}Table"
    count = used_structural_ids.get(base_identifier, 0) + 1
    used_structural_ids[base_identifier] = count
    identifier = base_identifier if count == 1 else f"{base_identifier}_{count}"
    return [{"name": identifier, "range_hint": "A1:C20"}]


def _formula_regions(name: str) -> list[dict[str, str]]:
    sheet_type = _sheet_type(name)
    if sheet_type == "summary":
        return [{"name": "SummaryCalculations", "range_hint": "E2:G20"}]
    if sheet_type == "dashboard":
        return [{"name": "DashboardMetrics", "range_hint": "E2:F10"}]
    return []


def _chart_regions(name: str) -> list[dict[str, str]]:
    if _sheet_type(name) == "dashboard":
        return [{"name": "PrimaryChart", "range_hint": "H2:M16"}]
    return []


def _validation_rules(name: str) -> list[dict[str, str]]:
    rules = [{"kind": "required_headers", "target": "A1:C1"}]
    if _sheet_type(name) == "raw_data":
        rules.append({"kind": "numeric_amounts", "target": "B2:B1048576"})
    return rules


def _build_batches(*, sheets: list[dict[str, Any]], build_batch_size: int) -> list[dict[str, Any]]:
    batch_size = max(int(build_batch_size or 1), 1)
    batches: list[dict[str, Any]] = []
    for start in range(0, len(sheets), batch_size):
        batch_sheets = sheets[start:start + batch_size]
        batches.append(
            {
                "index": len(batches),
                "sheet_start": start + 1,
                "sheet_end": start + len(batch_sheets),
                "sheet_names": [str(sheet.get("name", "") or "") for sheet in batch_sheets],
                # Keep generic workflow counters stable while XLSX-specific keys are introduced.
                "slide_start": start + 1,
                "slide_end": start + len(batch_sheets),
                "slide_titles": [str(sheet.get("name", "") or "") for sheet in batch_sheets],
                "slide_roles": [str(sheet.get("sheet_type", "") or "") for sheet in batch_sheets],
            }
        )
    return batches or [
        {
            "index": 0,
            "sheet_start": 0,
            "sheet_end": 0,
            "sheet_names": [],
            "slide_start": 0,
            "slide_end": 0,
            "slide_titles": [],
            "slide_roles": [],
        }
    ]


def _normalize_batches(
    raw_batches: Any,
    sheets: list[dict[str, Any]],
    *,
    build_batch_size: int,
) -> list[dict[str, Any]]:
    if not isinstance(raw_batches, list) or not raw_batches:
        return _build_batches(sheets=sheets, build_batch_size=build_batch_size)

    normalized: list[dict[str, Any]] = []
    sheet_names = [str(sheet.get("name", "") or "") for sheet in sheets]
    sheet_count = len(sheet_names)
    for index, batch in enumerate(raw_batches):
        if not isinstance(batch, dict):
            return _build_batches(sheets=sheets, build_batch_size=build_batch_size)
        start = _coerce_int(batch.get("sheet_start"), default=0)
        end = _coerce_int(batch.get("sheet_end"), default=0)
        names = _coerce_batch_list(batch, "sheet_names")
        if start <= 0 or end < start or end > sheet_count or not names:
            return _build_batches(sheets=sheets, build_batch_size=build_batch_size)
        expected_slice = sheets[start - 1:end]
        expected_names = sheet_names[start - 1:end]
        if names != expected_names:
            return _build_batches(sheets=sheets, build_batch_size=build_batch_size)
        normalized.append(
            {
                "index": _coerce_int(batch.get("index"), default=index),
                "sheet_start": start,
                "sheet_end": end,
                "sheet_names": names,
                "slide_start": start,
                "slide_end": end,
                "slide_titles": [str(sheet.get("name", "") or "") for sheet in expected_slice],
                "slide_roles": [str(sheet.get("sheet_type", "") or "") for sheet in expected_slice],
            }
        )
    if not _has_full_batch_coverage(normalized, sheet_count=sheet_count):
        return _build_batches(sheets=sheets, build_batch_size=build_batch_size)
    return normalized


def _coerce_list_field(container: dict[str, Any], key: str, *, fallback: list[Any]) -> list[Any]:
    if key not in container:
        return list(fallback)
    value = container.get(key)
    if isinstance(value, list):
        return list(value)
    if value is None:
        return []
    if isinstance(value, str):
        return []
    return []


def _coerce_batch_list(container: dict[str, Any], key: str) -> list[str]:
    value = container.get(key)
    if isinstance(value, list):
        return [str(item or "") for item in value if str(item or "")]
    return []


def _coerce_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _has_full_batch_coverage(batches: list[dict[str, Any]], *, sheet_count: int) -> bool:
    if sheet_count <= 0:
        return not batches
    cursor = 1
    for batch in batches:
        start = _coerce_int(batch.get("sheet_start"), default=0)
        end = _coerce_int(batch.get("sheet_end"), default=0)
        if start != cursor or end < start:
            return False
        cursor = end + 1
    return cursor == sheet_count + 1


def _structural_identifier(value: str) -> str:
    identifier = "".join(char for char in str(value or "") if char.isalnum())
    if not identifier:
        return "Sheet"
    if identifier[0].isdigit():
        return f"tbl_{identifier}"
    return identifier


def _seed_structural_ids_from_regions(
    regions: list[dict[str, Any]],
    *,
    used_structural_ids: dict[str, int],
) -> None:
    for region in regions:
        if not isinstance(region, dict):
            continue
        name = str(region.get("name", "") or "").strip()
        if not name:
            continue
        base_identifier = name
        suffix_count = 1
        if "_" in name:
            stem, suffix = name.rsplit("_", 1)
            if suffix.isdigit():
                base_identifier = stem
                suffix_count = int(suffix)
        used_structural_ids[base_identifier] = max(used_structural_ids.get(base_identifier, 0), suffix_count)


__all__ = ["XlsxStrategy"]
