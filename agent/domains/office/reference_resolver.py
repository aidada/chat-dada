from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent.domains.office.reference_models import (
    ConflictResolution,
    ExistingDocumentProfile,
    ReferenceStyleConstraints,
    ReferenceStructureConstraints,
    build_conflict_resolution,
)


def resolve_reference_constraints(
    *,
    goal_constraints: dict[str, Any],
    reference_structure_constraints: dict[str, Any],
    reference_style_constraints: dict[str, Any],
    existing_document_profile: dict[str, Any],
) -> dict[str, Any]:
    goal_payload = deepcopy(goal_constraints or {})
    structure_payload: ReferenceStructureConstraints = deepcopy(reference_structure_constraints or {})
    style_payload: ReferenceStyleConstraints = deepcopy(reference_style_constraints or {})
    document_payload: ExistingDocumentProfile = deepcopy(existing_document_profile or {})
    conflict_resolution: ConflictResolution = build_conflict_resolution()
    return {
        "goal_constraints": goal_payload,
        "reference_structure_constraints": structure_payload,
        "reference_style_constraints": style_payload,
        "existing_document_profile": document_payload,
        "conflict_resolution": conflict_resolution,
    }
