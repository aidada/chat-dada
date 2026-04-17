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
        format_name=str(structure_source.get("format", "") or ""),
        units=deepcopy(structure_source.get("units", []) or []),
    )
    style_payload: ReferenceStyleConstraints = build_reference_style_constraints(
        format_name=str(style_source.get("format", "") or ""),
        style_tokens=deepcopy(style_source.get("style_tokens", {}) or {}),
    )
    document_payload: ExistingDocumentProfile = build_existing_document_profile(
        format_name=str(document_source.get("format", "") or ""),
        units=deepcopy(document_source.get("units", []) or []),
        protected_units=deepcopy(document_source.get("protected_units", []) or []),
    )
    goal_source = dict(goal_constraints or {})
    goal_format_raw = str(goal_source.get("format", "") or "")
    goal_format_name = goal_format_raw if goal_format_raw.strip() else ""
    if not goal_format_name:
        structure_format = str(structure_payload.get("format", "") or "")
        goal_format_name = structure_format if structure_format.strip() else ""
    if not goal_format_name:
        document_format = str(document_payload.get("format", "") or "")
        goal_format_name = document_format if document_format.strip() else ""
    if not goal_format_name:
        style_format = str(style_payload.get("format", "") or "")
        goal_format_name = style_format if style_format.strip() else ""
    goal_payload: GoalConstraints = build_goal_constraints(
        format_name=goal_format_name,
        operation=str(goal_source.get("operation", "") or ""),
        goal=str(goal_source.get("goal", "") or ""),
        hard_requirements=deepcopy(goal_source.get("hard_requirements", []) or []),
    )
    conflict_resolution: ConflictResolution = build_conflict_resolution()
    return {
        "goal_constraints": goal_payload,
        "reference_structure_constraints": structure_payload,
        "reference_style_constraints": style_payload,
        "existing_document_profile": document_payload,
        "conflict_resolution": conflict_resolution,
    }
