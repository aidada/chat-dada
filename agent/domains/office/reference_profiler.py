from __future__ import annotations

import re
from typing import Any

from agent.domains.office.reference_models import (
    ReferenceStyleConstraints,
    ReferenceStructureConstraints,
    build_reference_style_constraints,
    build_reference_structure_constraints,
)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _append_name(candidate_names: list[str], seen: set[str], raw_name: str) -> None:
    name = re.sub(r"^[\s\-*•\d.)]+", "", _normalize_text(raw_name))
    name = re.sub(r"\s+", " ", name)
    if not name:
        return
    lowered = name.lower()
    if lowered in {"sheet", "sheets", "section", "sections", "worksheet", "worksheets", "document"}:
        return
    if lowered in seen:
        return
    seen.add(lowered)
    candidate_names.append(name)


def _extract_named_list(text: str, *, keywords: tuple[str, ...], allow_plain_lines: bool = False) -> list[str]:
    candidate_names: list[str] = []
    seen: set[str] = set()
    normalized_text = _normalize_text(text)
    if not normalized_text:
        return []

    for line in normalized_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(keyword in lowered for keyword in keywords) and ":" in stripped:
            _, tail = stripped.split(":", 1)
            for item in re.split(r"[,/|]", tail):
                _append_name(candidate_names, seen, item)
            continue
        if allow_plain_lines:
            _append_name(candidate_names, seen, stripped)

    return candidate_names


def _extract_xlsx_units(inspect_payload: dict[str, Any]) -> list[dict[str, Any]]:
    text = _normalize_text(inspect_payload.get("text"))

    candidate_names: list[str] = []
    seen: set[str] = set()
    for name in _extract_named_list(
        text,
        keywords=("sheet", "sheets", "worksheet", "tab", "tabs"),
        allow_plain_lines=False,
    ):
        _append_name(candidate_names, seen, name)
    return [{"name": name} for name in candidate_names]


def _extract_docx_units(inspect_payload: dict[str, Any]) -> list[dict[str, Any]]:
    text = _normalize_text(inspect_payload.get("text"))
    candidate_names: list[str] = []
    seen: set[str] = set()
    for name in _extract_named_list(
        text,
        keywords=("section", "sections", "heading", "headings", "chapter"),
        allow_plain_lines=True,
    ):
        _append_name(candidate_names, seen, name)
    return [{"name": name} for name in candidate_names]


def profile_reference_payload(*, format_name: str, inspect_payload: dict[str, Any]) -> dict[str, Any]:
    normalized_format_name = str(format_name or "").strip().lower()
    if normalized_format_name == "pptx":
        outline = inspect_payload.get("outline", [])
        stats = inspect_payload.get("stats", {})
        units = [
            {"name": title}
            for item in outline
            if isinstance(item, dict)
            if (title := str(item.get("title", "") or "").strip())
        ]
        structure: ReferenceStructureConstraints = build_reference_structure_constraints(
            format_name=normalized_format_name,
            units=units,
        )
        style: ReferenceStyleConstraints = build_reference_style_constraints(
            format_name=normalized_format_name,
            style_tokens=stats if isinstance(stats, dict) else {},
        )
        return {
            "structure": structure,
            "style": style,
        }
    if normalized_format_name == "xlsx":
        issues = inspect_payload.get("issues")
        style_tokens: dict[str, Any] = {}
        if isinstance(issues, dict):
            issue_summary = _normalize_text(issues.get("message")) or _normalize_text(issues.get("text"))
            issue_excerpt = _normalize_text(issues.get("text"))
            if issue_summary:
                style_tokens["issue_summary"] = issue_summary
            if issue_excerpt:
                style_tokens["issue_excerpt"] = issue_excerpt
        structure = build_reference_structure_constraints(
            format_name=normalized_format_name,
            units=_extract_xlsx_units(inspect_payload),
        )
        style = build_reference_style_constraints(
            format_name=normalized_format_name,
            style_tokens=style_tokens,
        )
        return {
            "structure": structure,
            "style": style,
        }
    if normalized_format_name == "docx":
        annotated = inspect_payload.get("annotated")
        style_tokens = {}
        if isinstance(annotated, dict):
            annotation_summary = _normalize_text(annotated.get("message")) or _normalize_text(annotated.get("text"))
            annotation_excerpt = _normalize_text(annotated.get("text"))
            if annotation_summary:
                style_tokens["annotation_summary"] = annotation_summary
            if annotation_excerpt:
                style_tokens["annotation_excerpt"] = annotation_excerpt
        structure = build_reference_structure_constraints(
            format_name=normalized_format_name,
            units=_extract_docx_units(inspect_payload),
        )
        style = build_reference_style_constraints(
            format_name=normalized_format_name,
            style_tokens=style_tokens,
        )
        return {
            "structure": structure,
            "style": style,
        }
    structure = build_reference_structure_constraints(format_name=normalized_format_name, units=[])
    style = build_reference_style_constraints(format_name=normalized_format_name, style_tokens={})
    return {
        "structure": structure,
        "style": style,
    }
