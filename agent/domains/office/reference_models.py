from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class GoalConstraints(TypedDict):
    format: str
    operation: str
    goal: str
    hard_requirements: list[str]


class ReferenceStructureConstraints(TypedDict):
    format: str
    units: list[dict[str, Any]]


class ReferenceStyleConstraints(TypedDict):
    format: str
    style_tokens: dict[str, Any]


class ExistingDocumentProfile(TypedDict):
    format: str
    units: list[dict[str, Any]]
    protected_units: list[str]


class ConflictResolution(TypedDict):
    priority_order: list[str]
    record_deviations: bool


class FidelityDeviation(TypedDict, total=False):
    kind: str
    message: str
    field: str
    expected: Any
    actual: Any
    details: dict[str, Any]


def build_goal_constraints(
    *,
    format_name: str,
    operation: str,
    goal: str,
    hard_requirements: list[str] | None = None,
) -> GoalConstraints:
    return {
        "format": str(format_name or ""),
        "operation": str(operation or ""),
        "goal": str(goal or "").strip(),
        "hard_requirements": list(hard_requirements or []),
    }


def build_reference_structure_constraints(
    *,
    format_name: str,
    units: list[dict[str, Any]] | None = None,
) -> ReferenceStructureConstraints:
    return {
        "format": str(format_name or "").lower(),
        "units": list(units or []),
    }


def build_reference_style_constraints(
    *,
    format_name: str,
    style_tokens: dict[str, Any] | None = None,
) -> ReferenceStyleConstraints:
    return {
        "format": str(format_name or "").lower(),
        "style_tokens": dict(style_tokens or {}),
    }


def build_existing_document_profile(
    *,
    format_name: str,
    units: list[dict[str, Any]] | None = None,
    protected_units: list[str] | None = None,
) -> ExistingDocumentProfile:
    return {
        "format": str(format_name or "").lower(),
        "units": list(units or []),
        "protected_units": list(protected_units or []),
    }


def build_conflict_resolution() -> ConflictResolution:
    return {
        "priority_order": ["goal", "reference"],
        "record_deviations": True,
    }
