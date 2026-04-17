from __future__ import annotations

from typing import Any

from agent.domains.office.reference_models import build_conflict_resolution


def resolve_reference_constraints(
    *,
    goal_constraints: dict[str, Any],
    reference_structure_constraints: dict[str, Any],
    reference_style_constraints: dict[str, Any],
    existing_document_profile: dict[str, Any],
) -> dict[str, Any]:
    return {
        "goal_constraints": dict(goal_constraints or {}),
        "reference_structure_constraints": dict(reference_structure_constraints or {}),
        "reference_style_constraints": dict(reference_style_constraints or {}),
        "existing_document_profile": dict(existing_document_profile or {}),
        "conflict_resolution": build_conflict_resolution(),
    }
