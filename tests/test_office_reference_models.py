from agent.domains.office.reference_models import (
    build_conflict_resolution,
    build_existing_document_profile,
    build_goal_constraints,
    build_reference_style_constraints,
    build_reference_structure_constraints,
)


def test_build_goal_constraints_normalizes_case_and_copies_lists() -> None:
    hard_requirements = ["preserve formulas", "rename summary sheet"]

    payload = build_goal_constraints(
        format_name="XLSX",
        operation="EDIT",
        goal="按用户规范修改预算表",
        hard_requirements=hard_requirements,
    )

    hard_requirements.append("add chart")

    assert payload["format"] == "xlsx"
    assert payload["operation"] == "edit"
    assert payload["goal"] == "按用户规范修改预算表"
    assert payload["hard_requirements"] == ["preserve formulas", "rename summary sheet"]
    assert payload["hard_requirements"] is not hard_requirements


def test_build_reference_structure_constraints_normalizes_and_copies_units() -> None:
    units = [{"name": "Executive Summary"}]

    payload = build_reference_structure_constraints(
        format_name="DOCX",
        units=units,
    )

    units.append({"name": "Appendix"})

    assert payload["format"] == "docx"
    assert payload["units"] == [{"name": "Executive Summary"}]
    assert len(units) == 2
    assert payload["units"] is not units


def test_build_reference_style_constraints_normalizes_and_copies_tokens() -> None:
    style_tokens = {"tone": "Formal", "spacing": "1.15"}

    payload = build_reference_style_constraints(
        format_name="PPTX",
        style_tokens=style_tokens,
    )

    style_tokens["tone"] = "casual"
    payload["style_tokens"]["spacing"] = "1.5"

    assert payload["format"] == "pptx"
    assert payload["style_tokens"] == {"tone": "Formal", "spacing": "1.5"}
    assert style_tokens == {"tone": "casual", "spacing": "1.15"}
    assert payload["style_tokens"] is not style_tokens


def test_build_existing_document_profile_tracks_protected_units_and_copies_inputs() -> None:
    units = [{"name": "Executive Summary"}]
    protected_units = ["Appendix"]

    profile = build_existing_document_profile(
        format_name="DOCX",
        units=units,
        protected_units=protected_units,
    )

    units.append({"name": "Appendix"})
    protected_units.append("References")

    assert profile["format"] == "docx"
    assert profile["units"] == [{"name": "Executive Summary"}]
    assert profile["protected_units"] == ["Appendix"]
    assert profile["units"] is not units
    assert profile["protected_units"] is not protected_units


def test_build_conflict_resolution_defaults_to_goal_first() -> None:
    resolution = build_conflict_resolution()

    assert resolution["priority_order"] == ["goal", "reference"]
    assert resolution["record_deviations"] is True
