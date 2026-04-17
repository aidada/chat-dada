from __future__ import annotations

from typing import Any


def build_goal_constraints(
    *,
    format_name: str,
    operation: str,
    goal: str,
    hard_requirements: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "format": str(format_name or "").lower(),
        "operation": str(operation or "").lower(),
        "goal": str(goal or "").strip(),
        "hard_requirements": list(hard_requirements or []),
    }


def build_reference_structure_constraints(
    *,
    format_name: str,
    units: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "format": str(format_name or "").lower(),
        "units": list(units or []),
    }


def build_reference_style_constraints(
    *,
    format_name: str,
    style_tokens: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "format": str(format_name or "").lower(),
        "style_tokens": dict(style_tokens or {}),
    }


def build_existing_document_profile(
    *,
    format_name: str,
    units: list[dict[str, Any]] | None = None,
    protected_units: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "format": str(format_name or "").lower(),
        "units": list(units or []),
        "protected_units": list(protected_units or []),
    }


def build_conflict_resolution() -> dict[str, Any]:
    return {
        "priority_order": ["goal", "reference"],
        "record_deviations": True,
    }
