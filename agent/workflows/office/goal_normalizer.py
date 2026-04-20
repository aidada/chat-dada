from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from core.models import get_llm
from agent.workflows.office.goal_contract import (
    GoalNormalizationRequest,
    GoalProfile,
    NeedClarification,
    NormalizeOk,
    QualityProfile,
    RejectNormalization,
)

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
REQUESTED_SLIDE_COUNT_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:页|page(?:s)?|slide(?:s)?)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
SLIDE_COUNT_INTENT_RE = re.compile(
    r"(页|slides?|pages?|十来页|十几页|几十页|多页|几页)",
    re.IGNORECASE,
)
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


class _StructuredGoalExtraction(BaseModel):
    format: str | None = None
    operation: str | None = None
    requested_slide_count: int | None = Field(default=None, ge=1, le=30)
    output_filename: str | None = None
    quality_profile: QualityProfile = Field(default_factory=QualityProfile)
    confidence: str = "low"
    missing_fields: list[str] = Field(default_factory=list)


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


async def normalize_goal_profile(request: GoalNormalizationRequest) -> NormalizeOk | NeedClarification | RejectNormalization:
    normalized_source_files = normalize_reference_files(request.source_files)
    normalized_reference_files = normalize_reference_files(request.reference_files)
    raw_user_message = str(request.raw_user_message or "").strip()
    orchestrator_summary = str(request.orchestrator_summary or "").strip()
    file_hint = str(request.file_hint or "").strip()

    raw_format = infer_format(
        raw_user_message,
        file_hint,
        normalized_source_files,
        str(request.explicit_format or ""),
    )
    summary_format = infer_format(
        orchestrator_summary,
        file_hint,
        normalized_source_files,
        str(request.explicit_format or ""),
    )
    raw_operation = infer_operation(
        raw_user_message,
        normalized_source_files,
        str(request.explicit_operation or ""),
    )
    summary_operation = infer_operation(
        orchestrator_summary,
        normalized_source_files,
        str(request.explicit_operation or ""),
    )

    if not _looks_like_office_request(
        raw_user_message=raw_user_message,
        orchestrator_summary=orchestrator_summary,
        file_hint=file_hint,
        source_files=normalized_source_files,
        reference_files=normalized_reference_files,
        explicit_format=str(request.explicit_format or ""),
    ):
        return RejectNormalization(reason="当前请求看起来不是 Office 文档任务。")

    extracted = await _extract_goal_contract(
        raw_user_message=raw_user_message,
        orchestrator_summary=orchestrator_summary,
        file_hint=file_hint,
        source_files=normalized_source_files,
        reference_files=normalized_reference_files,
        clarification_history=request.clarification_history,
    )

    format_name = raw_format or _normalize_format(extracted.format) or summary_format or None
    operation = raw_operation or _normalize_operation(extracted.operation) or summary_operation or None

    raw_slide_count = infer_requested_slide_count(raw_user_message) if format_name == "pptx" else None
    raw_slide_intent = format_name == "pptx" and _has_slide_count_intent(raw_user_message)
    requested_slide_count = raw_slide_count
    if requested_slide_count is None and not raw_slide_intent:
        requested_slide_count = _normalize_slide_count(extracted.requested_slide_count)

    merged_quality = _merge_quality_profiles(
        inferred=infer_quality_profile(raw_user_message, format_name=format_name or ""),
        structured=extracted.quality_profile,
        format_name=format_name or "",
    )
    output_filename = _resolve_output_filename(
        raw_user_message=raw_user_message,
        orchestrator_summary=orchestrator_summary,
        file_hint=file_hint,
        format_name=format_name or "",
        operation=operation,
        structured_output_filename=extracted.output_filename,
    )

    missing_fields: list[str] = []
    if format_name is None:
        missing_fields.append("format")
    if operation is None:
        missing_fields.append("operation")
    if operation in {"edit", "inspect", "transform"} and not normalized_source_files:
        missing_fields.append("source_files")
    if format_name == "pptx" and raw_slide_intent and requested_slide_count is None:
        missing_fields.append("requested_slide_count")
    for item in extracted.missing_fields:
        normalized = str(item or "").strip()
        if normalized and normalized not in missing_fields:
            if normalized == "requested_slide_count" and (format_name != "pptx" or requested_slide_count is not None):
                continue
            missing_fields.append(normalized)

    if missing_fields:
        return NeedClarification(
            questions=_build_clarification_questions(missing_fields, operation=operation),
            missing_fields=missing_fields,
        )

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
    )
    return NormalizeOk(profile=profile)


async def _extract_goal_contract(
    *,
    raw_user_message: str,
    orchestrator_summary: str,
    file_hint: str,
    source_files: list[str],
    reference_files: list[str],
    clarification_history: list[dict[str, Any]],
) -> _StructuredGoalExtraction:
    try:
        llm = get_llm("orchestrator")
        structured_llm = llm.with_structured_output(_StructuredGoalExtraction)
        response = await structured_llm.ainvoke(
            [
                SystemMessage(
                    content=(
                        "You normalize Office workflow goals into a strict schema. "
                        "Treat raw_user_message as authoritative. "
                        "Treat orchestrator_summary only as supporting context and never let it override the raw user commitment. "
                        "Return the schema only."
                    )
                ),
                HumanMessage(
                    content=_build_structured_prompt(
                        raw_user_message=raw_user_message,
                        orchestrator_summary=orchestrator_summary,
                        file_hint=file_hint,
                        source_files=source_files,
                        reference_files=reference_files,
                        clarification_history=clarification_history,
                    )
                ),
            ]
        )
    except Exception:
        return _StructuredGoalExtraction()

    if isinstance(response, _StructuredGoalExtraction):
        return response
    if isinstance(response, dict):
        return _StructuredGoalExtraction(**response)
    if hasattr(response, "model_dump"):
        return _StructuredGoalExtraction(**response.model_dump())
    return _StructuredGoalExtraction()


def _build_structured_prompt(
    *,
    raw_user_message: str,
    orchestrator_summary: str,
    file_hint: str,
    source_files: list[str],
    reference_files: list[str],
    clarification_history: list[dict[str, Any]],
) -> str:
    history_lines = []
    for item in clarification_history or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "") or item.get("content", "") or "").strip()
        answer = str(item.get("answer", "") or "").strip()
        if question or answer:
            history_lines.append(f"- question={question} answer={answer}")
    return "\n".join(
        [
            f"raw_user_message:\n{raw_user_message or '(empty)'}",
            f"\norchestrator_summary:\n{orchestrator_summary or '(empty)'}",
            f"\nfile_hint: {file_hint or '(empty)'}",
            f"source_files: {source_files}",
            f"reference_files: {reference_files}",
            "clarification_history:",
            *(history_lines or ["- (empty)"]),
            "",
            "Fill the schema from the user request.",
            "Use null for unknown values.",
            "missing_fields must contain only fields you cannot infer confidently.",
        ]
    )


def _looks_like_office_request(
    *,
    raw_user_message: str,
    orchestrator_summary: str,
    file_hint: str,
    source_files: list[str],
    reference_files: list[str],
    explicit_format: str,
) -> bool:
    if _normalize_format(explicit_format):
        return True
    if infer_format(raw_user_message, file_hint, source_files, explicit_format):
        return True
    if infer_format(orchestrator_summary, file_hint, source_files, explicit_format):
        return True
    for item in [file_hint, *source_files, *reference_files]:
        if Path(str(item or "")).suffix.lower().lstrip(".") in {"pptx", "docx", "xlsx"}:
            return True
    return False


def _normalize_format(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in {"pptx", "docx", "xlsx"} else None


def _normalize_operation(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in {"create", "edit", "inspect", "transform"} else None


def _normalize_slide_count(value: Any) -> int | None:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if 1 <= count <= 30 else None


def _merge_quality_profiles(
    *,
    inferred: dict[str, Any],
    structured: QualityProfile,
    format_name: str,
) -> QualityProfile:
    structured_data = structured.model_dump() if hasattr(structured, "model_dump") else dict(structured or {})
    return QualityProfile(
        animations=bool(inferred.get("animations")) or bool(structured_data.get("animations")),
        visuals=bool(inferred.get("visuals")) or bool(structured_data.get("visuals")),
        notes=(format_name == "pptx") or bool(structured_data.get("notes")),
    )


def _resolve_output_filename(
    *,
    raw_user_message: str,
    orchestrator_summary: str,
    file_hint: str,
    format_name: str,
    operation: str | None,
    structured_output_filename: str | None,
) -> str | None:
    if operation != "create" or not format_name:
        return None

    heuristic = infer_default_create_file(raw_user_message or orchestrator_summary, file_hint, format_name)
    explicit = extract_explicit_filename(str(structured_output_filename or ""), format_name)
    if explicit and not is_generic_generated_filename(explicit):
        return explicit
    return heuristic or None


def _resolve_confidence(
    *,
    extracted_confidence: Any,
    raw_slide_count: int | None,
    extracted_slide_count: int | None,
) -> str:
    normalized = str(extracted_confidence or "").strip().lower()
    if raw_slide_count is not None and extracted_slide_count == raw_slide_count:
        return "high"
    if raw_slide_count is not None:
        return "medium"
    if normalized in {"high", "medium", "low"}:
        return normalized
    return "low"


def _has_slide_count_intent(text: str) -> bool:
    return bool(SLIDE_COUNT_INTENT_RE.search(str(text or "")))


def _build_clarification_questions(missing_fields: list[str], *, operation: str | None) -> list[str]:
    questions: list[str] = []
    for field in missing_fields:
        if field == "format":
            questions.append("你要的是 PPT、Word 还是 Excel？")
        elif field == "source_files" and operation in {"edit", "inspect", "transform"}:
            questions.append("请上传要处理的 Office 文件，或提供明确的 .pptx / .docx / .xlsx 路径。")
        elif field == "requested_slide_count":
            questions.append("你希望这份 PPT 大约多少页？")
        elif field == "operation":
            questions.append("你是要创建新文档，还是编辑、检查、转换现有文档？")
    return questions


__all__ = [
    "OFFICE_INNER_RECURSION_LIMIT",
    "compute_dynamic_inner_limit",
    "extract_explicit_filename",
    "infer_build_batch_size",
    "infer_default_create_file",
    "infer_format",
    "infer_operation",
    "infer_quality_profile",
    "infer_requested_slide_count",
    "is_generic_generated_filename",
    "normalize_goal_profile",
    "normalize_reference_files",
    "refine_filename_from_plan",
]
