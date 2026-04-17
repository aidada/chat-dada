from __future__ import annotations

import re
from pathlib import Path
from typing import Any

OFFICE_INNER_RECURSION_LIMIT = 40

EDIT_HINTS = (
    "修改",
    "编辑",
    "更新",
    "改写",
    "润色",
    "replace",
    "update",
    "edit",
    "fix",
)
INSPECT_HINTS = (
    "查看",
    "检查",
    "分析",
    "读取",
    "提取",
    "总结",
    "inspect",
    "review",
    "analyze",
    "read",
)
TRANSFORM_HINTS = (
    "转换",
    "导出",
    "另存为",
    "转成",
    "convert",
    "export",
)
CREATE_HINTS = (
    "创建",
    "生成",
    "制作",
    "做",
    "写",
    "draft",
    "create",
    "generate",
)

EXPLICIT_FILENAME_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:pptx|docx|xlsx))\b", re.IGNORECASE)
ASCII_FILENAME_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
REQUESTED_SLIDE_COUNT_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:页|page(?:s)?|slide(?:s)?)(?!\w)", re.IGNORECASE)
FILENAME_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "for",
    "to",
    "of",
    "in",
    "on",
    "with",
    "create",
    "generate",
    "make",
    "draft",
    "write",
    "ppt",
    "pptx",
    "doc",
    "docx",
    "xlsx",
    "excel",
    "word",
    "presentation",
    "document",
    "workbook",
    "download",
    "downloads",
    "folder",
    "file",
}
INTENT_FILENAME_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("介绍", "intro", "overview"), "intro"),
    (("指南", "guide", "tutorial", "使用"), "guide"),
    (("总结", "summary"), "summary"),
    (("报告", "report"), "report"),
    (("方案", "proposal"), "proposal"),
    (("计划", "plan"), "plan"),
    (("分析", "analysis"), "analysis"),
    (("痛点", "pain point", "pain points"), "pain-points"),
    (("dashboard", "仪表盘", "kpi"), "dashboard"),
    (("research", "研究"), "research"),
    (("patent", "专利"), "patent"),
    (("financial", "finance", "财务"), "financial-model"),
    (("education", "learning", "study", "teaching", "教育", "学习"), "education"),
    (("children", "child", "kids", "student", "students", "孩子", "儿童", "青少年"), "children"),
    (("modern", "modernization", "new era", "新时代", "现代化"), "modern"),
)
FORMAT_DEFAULT_FILENAMES = {
    "pptx": "presentation",
    "docx": "document",
    "xlsx": "workbook",
}
GENERIC_FILENAME_STEMS = {
    "ai",
    "deck",
    "slides",
    "slide-deck",
    "presentation",
    "document",
    "workbook",
    "file",
    "output",
    "result",
    "demo",
    "temp",
    "untitled",
    "new",
}


def infer_format(goal: str, file_hint: str, source_files: list[str], explicit: str) -> str:
    explicit_lower = str(explicit or "").strip().lower()
    if explicit_lower in {"pptx", "docx", "xlsx"}:
        return explicit_lower

    candidates = [file_hint, *source_files]
    for item in candidates:
        suffix = Path(str(item or "")).suffix.lower().lstrip(".")
        if suffix in {"pptx", "docx", "xlsx"}:
            return suffix

    lowered = str(goal or "").lower()
    if any(keyword in lowered for keyword in ("ppt", "powerpoint", "presentation", "deck", "幻灯片", "演示文稿")):
        return "pptx"
    if any(keyword in lowered for keyword in ("docx", "word", "memo", "letter", "manuscript", "proposal", "报告")):
        return "docx"
    if any(keyword in lowered for keyword in ("xlsx", "excel", "spreadsheet", "workbook", "dashboard", "表格", "电子表格")):
        return "xlsx"
    return ""


def infer_operation(goal: str, source_files: list[str], explicit: str) -> str:
    explicit_lower = str(explicit or "").strip().lower()
    if explicit_lower in {"create", "edit", "inspect", "transform"}:
        return explicit_lower

    lowered = str(goal or "").lower()
    if any(keyword in lowered for keyword in TRANSFORM_HINTS):
        return "transform"
    if any(keyword in lowered for keyword in EDIT_HINTS):
        return "edit"
    if any(keyword in lowered for keyword in INSPECT_HINTS):
        return "inspect"
    if any(keyword in lowered for keyword in CREATE_HINTS):
        return "create"
    return "inspect" if source_files else "create"


def extract_explicit_filename(text: str, format_name: str) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None

    suffix = Path(raw).suffix.lower().lstrip(".")
    if suffix in {"pptx", "docx", "xlsx"}:
        candidate = Path(raw).name
        if format_name and suffix != format_name:
            return None
        return candidate

    match = EXPLICIT_FILENAME_RE.search(raw)
    if not match:
        return None

    candidate = Path(match.group(1)).name
    suffix = Path(candidate).suffix.lower().lstrip(".")
    if format_name and suffix != format_name:
        return None
    return candidate


def infer_default_create_file(goal: str, file_hint: str, format_name: str) -> str:
    if not format_name:
        return ""

    explicit_goal = extract_explicit_filename(goal, format_name)
    if explicit_goal:
        return explicit_goal

    explicit_hint = extract_explicit_filename(file_hint, format_name)
    if explicit_hint and not is_generic_generated_filename(explicit_hint):
        return explicit_hint

    lowered = str(goal or "").lower()
    stem_parts: list[str] = []

    for token in ASCII_FILENAME_TOKEN_RE.findall(lowered):
        normalized = token.strip("-").lower()
        if (
            not normalized
            or normalized in FILENAME_STOPWORDS
            or normalized.isdigit()
            or normalized.endswith((".pptx", ".docx", ".xlsx"))
        ):
            continue
        if normalized not in stem_parts:
            stem_parts.append(normalized)
        if len(stem_parts) >= 2:
            break

    for keywords, label in INTENT_FILENAME_HINTS:
        if any(keyword in lowered for keyword in keywords) and label not in stem_parts:
            stem_parts.append(label)
        if len(stem_parts) >= 3:
            break

    if not stem_parts:
        stem_parts.append(FORMAT_DEFAULT_FILENAMES.get(format_name, "office-file"))

    stem = "-".join(stem_parts[:3])
    stem = re.sub(r"[^a-z0-9_-]+", "-", stem).strip("-_")
    if not stem:
        stem = FORMAT_DEFAULT_FILENAMES.get(format_name, "office-file")
    stem = stem[:64].rstrip("-_") or FORMAT_DEFAULT_FILENAMES.get(format_name, "office-file")
    return f"{stem}.{format_name}"


def is_generic_generated_filename(filename: str) -> bool:
    stem = re.sub(r"[^a-z0-9_-]+", "-", Path(str(filename or "")).stem.lower()).strip("-_")
    if not stem:
        return True
    if stem in GENERIC_FILENAME_STEMS:
        return True
    parts = [part for part in re.split(r"[-_]+", stem) if part]
    return len(parts) == 1 and parts[0] in GENERIC_FILENAME_STEMS


def infer_requested_slide_count(goal: str) -> int | None:
    match = REQUESTED_SLIDE_COUNT_RE.search(str(goal or ""))
    if not match:
        return None
    try:
        count = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return count if 1 <= count <= 30 else None


def normalize_reference_files(reference_files: list[str]) -> list[str]:
    normalized: list[str] = []
    for item in reference_files or []:
        candidate = str(item or "").strip()
        if not candidate or candidate in normalized:
            continue
        normalized.append(candidate)
    return normalized


def infer_quality_profile(goal: str, *, format_name: str) -> dict[str, Any]:
    lowered = str(goal or "").lower()
    wants_animations = any(token in lowered for token in ("动画", "animation", "animated", "transition", "转场"))
    wants_visuals = any(token in lowered for token in ("图文并茂", "配图", "图片", "image", "visual", "插图", "图表", "chart"))
    wants_notes = format_name == "pptx"
    return {
        "animations": wants_animations,
        "visuals": wants_visuals,
        "notes": wants_notes,
    }


def infer_build_batch_size(
    *,
    format_name: str,
    operation: str,
    requested_slide_count: int | None,
) -> int:
    if format_name != "pptx" or operation not in {"create", "transform"}:
        return 1
    if requested_slide_count is None:
        return 2
    if requested_slide_count >= 8:
        return 3
    if requested_slide_count >= 5:
        return 2
    return 1


def compute_dynamic_inner_limit(
    *,
    format_name: str,
    operation: str,
    requested_slide_count: int | None,
    quality_profile: dict[str, Any],
) -> int:
    if format_name != "pptx" or operation not in {"create", "transform"}:
        return OFFICE_INNER_RECURSION_LIMIT

    slide_count = int(requested_slide_count or 0)
    limit = 24 + max(slide_count, 1) * 6
    if bool(quality_profile.get("animations")):
        limit += 8
    if bool(quality_profile.get("visuals")):
        limit += 6
    if bool(quality_profile.get("notes")):
        limit += 4
    return max(limit, OFFICE_INNER_RECURSION_LIMIT)


def refine_filename_from_plan(*, current_filename: str, plan_title: str, format_name: str) -> str:
    if not format_name:
        return current_filename
    current = str(current_filename or "").strip()
    if current and not is_generic_generated_filename(current):
        return current
    title = str(plan_title or "").strip().lower()
    stem_parts: list[str] = []
    for token in ASCII_FILENAME_TOKEN_RE.findall(title):
        normalized = token.strip("-").lower()
        if (
            not normalized
            or normalized in FILENAME_STOPWORDS
            or normalized.isdigit()
            or normalized in stem_parts
        ):
            continue
        stem_parts.append(normalized)
        if len(stem_parts) >= 4:
            break
    for keywords, label in INTENT_FILENAME_HINTS:
        if any(keyword in title for keyword in keywords) and label not in stem_parts:
            stem_parts.append(label)
        if len(stem_parts) >= 4:
            break
    if not stem_parts:
        return infer_default_create_file(plan_title, current_filename, format_name)
    stem = "-".join(stem_parts[:4]).strip("-_")
    return f"{stem[:64].rstrip('-_') or FORMAT_DEFAULT_FILENAMES.get(format_name, 'office-file')}.{format_name}"


def normalize_goal_profile(
    *,
    goal: str,
    file_hint: str,
    source_files: list[str],
    reference_files: list[str],
    explicit_format: str,
    explicit_operation: str,
) -> dict[str, Any]:
    format_name = infer_format(goal, file_hint, source_files, explicit_format)
    operation = infer_operation(goal, source_files, explicit_operation)
    normalized_reference_files = normalize_reference_files(reference_files)
    default_create_file = infer_default_create_file(goal, file_hint, format_name) if operation == "create" else ""
    requested_slide_count = infer_requested_slide_count(goal) if format_name == "pptx" else None
    quality_profile = infer_quality_profile(goal, format_name=format_name)
    build_batch_size = infer_build_batch_size(
        format_name=format_name,
        operation=operation,
        requested_slide_count=requested_slide_count,
    )
    inner_limit = compute_dynamic_inner_limit(
        format_name=format_name,
        operation=operation,
        requested_slide_count=requested_slide_count,
        quality_profile=quality_profile,
    )
    return {
        "format": format_name,
        "operation": operation,
        "default_create_file": default_create_file,
        "requested_slide_count": requested_slide_count or 0,
        "reference_files": normalized_reference_files,
        "quality_profile": quality_profile,
        "build_batch_size": build_batch_size,
        "inner_recursion_limit": inner_limit,
    }
