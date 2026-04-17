from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent.domains.office.reference_models import (
    ConflictResolution,
    ExistingDocumentProfile,
    ReferenceStyleConstraints,
    ReferenceStructureConstraints,
    build_conflict_resolution,
    build_existing_document_profile,
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
    goal_payload = deepcopy(goal_constraints or {})
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
    conflict_resolution: ConflictResolution = build_conflict_resolution()
    return {
        "goal_constraints": goal_payload,
        "reference_structure_constraints": structure_payload,
        "reference_style_constraints": style_payload,
        "existing_document_profile": document_payload,
        "conflict_resolution": conflict_resolution,
    }
