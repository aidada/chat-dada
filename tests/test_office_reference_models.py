from agent.domains.office.reference_models import (
    build_conflict_resolution,
    build_existing_document_profile,
    build_goal_constraints,
    build_reference_style_constraints,
    build_reference_structure_constraints,
)


def test_build_goal_constraints_goal_wins_over_reference() -> None:
    payload = build_goal_constraints(
        format_name="xlsx",
        operation="edit",
        goal="按用户规范修改预算表",
        hard_requirements=["preserve formulas", "rename summary sheet"],
    )

    assert payload["format"] == "xlsx"
    assert payload["operation"] == "edit"
    assert payload["hard_requirements"] == ["preserve formulas", "rename summary sheet"]


def test_build_conflict_resolution_defaults_to_goal_first() -> None:
    resolution = build_conflict_resolution()

    assert resolution["priority_order"] == ["goal", "reference"]
    assert resolution["record_deviations"] is True


def test_build_existing_document_profile_tracks_protected_units() -> None:
    profile = build_existing_document_profile(
        format_name="docx",
        units=[{"name": "Executive Summary"}],
        protected_units=["Appendix"],
    )

    assert profile["format"] == "docx"
    assert profile["protected_units"] == ["Appendix"]
