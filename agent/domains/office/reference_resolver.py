from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent.domains.office.reference_models import (
    ConflictResolution,
    ExistingDocumentProfile,
    GoalConstraints,
    ReferenceStyleConstraints,
    ReferenceStructureConstraints,
    build_conflict_resolution,
    build_existing_document_profile,
    build_goal_constraints,
    build_reference_style_constraints,
    build_reference_structure_constraints,
)


def _canonical_format(value: Any) -> str:
    return str(value or "").strip().lower()


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _derive_docx_goal_lists(goal_source: dict[str, Any]) -> tuple[list[str], list[str]]:
    explicit_headings = _coerce_string_list(goal_source.get("section_headings"))
    explicit_formatting = _coerce_string_list(goal_source.get("formatting_instructions"))
    if explicit_headings or explicit_formatting:
        return explicit_headings, explicit_formatting
    return [], []

def resolve_reference_constraints(
    *,
    goal_constraints: dict[str, Any],
    reference_structure_constraints: dict[str, Any],
    reference_style_constraints: dict[str, Any],
    existing_document_profile: dict[str, Any],
) -> dict[str, Any]:
    structure_source = dict(reference_structure_constraints or {})
    style_source = dict(reference_style_constraints or {})
    document_source = dict(existing_document_profile or {})
    structure_payload: ReferenceStructureConstraints = build_reference_structure_constraints(
        format_name=_canonical_format(structure_source.get("format", "")),
        units=deepcopy(structure_source.get("units", []) or []),
    )
    style_payload: ReferenceStyleConstraints = build_reference_style_constraints(
        format_name=_canonical_format(style_source.get("format", "")),
        style_tokens=deepcopy(style_source.get("style_tokens", {}) or {}),
    )
    document_payload: ExistingDocumentProfile = build_existing_document_profile(
        format_name=_canonical_format(document_source.get("format", "")),
        units=deepcopy(document_source.get("units", []) or []),
        protected_units=deepcopy(document_source.get("protected_units", []) or []),
    )
    goal_source = dict(goal_constraints or {})
    goal_format_name = _canonical_format(goal_source.get("format", ""))
    if not goal_format_name:
        structure_format = _canonical_format(structure_payload.get("format", ""))
        goal_format_name = structure_format
    if not goal_format_name:
        document_format = _canonical_format(document_payload.get("format", ""))
        goal_format_name = document_format
    if not goal_format_name:
        style_format = _canonical_format(style_payload.get("format", ""))
        goal_format_name = style_format
    section_headings: list[str] = []
    formatting_instructions: list[str] = []
    if goal_format_name == "docx":
        section_headings, formatting_instructions = _derive_docx_goal_lists(goal_source)
    else:
        section_headings = _coerce_string_list(goal_source.get("section_headings"))
        formatting_instructions = _coerce_string_list(goal_source.get("formatting_instructions"))
    goal_payload: GoalConstraints = build_goal_constraints(
        format_name=goal_format_name,
        operation=str(goal_source.get("operation", "") or ""),
        goal=str(goal_source.get("goal", "") or ""),
        hard_requirements=deepcopy(goal_source.get("hard_requirements", []) or []),
        section_headings=section_headings,
        formatting_instructions=formatting_instructions,
    )
    conflict_resolution: ConflictResolution = build_conflict_resolution()
    return {
        "goal_constraints": goal_payload,
        "reference_structure_constraints": structure_payload,
        "reference_style_constraints": style_payload,
        "existing_document_profile": document_payload,
        "conflict_resolution": conflict_resolution,
    }
