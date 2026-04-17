from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent.domains.office.reference_models import (
    ReferenceStyleConstraints,
    ReferenceStructureConstraints,
    build_reference_style_constraints,
    build_reference_structure_constraints,
)


def profile_reference_payload(*, format_name: str, inspect_payload: dict[str, Any]) -> dict[str, Any]:
    if format_name == "pptx":
        outline = inspect_payload.get("outline", [])
        stats = inspect_payload.get("stats", {})
        units = [
            {"name": str(item.get("title", "") or "")}
            for item in outline
            if isinstance(item, dict)
        ]
        structure: ReferenceStructureConstraints = build_reference_structure_constraints(
            format_name=format_name,
            units=units,
        )
        style: ReferenceStyleConstraints = build_reference_style_constraints(
            format_name=format_name,
            style_tokens=deepcopy(stats if isinstance(stats, dict) else {}),
        )
        return {
            "structure": structure,
            "style": style,
        }
    structure = build_reference_structure_constraints(format_name=format_name, units=[])
    style = build_reference_style_constraints(format_name=format_name, style_tokens={})
    return {
        "structure": structure,
        "style": style,
    }
