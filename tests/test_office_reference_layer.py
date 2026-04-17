from agent.domains.office.reference_profiler import profile_reference_payload
from agent.domains.office.reference_resolver import resolve_reference_constraints


def test_profile_reference_payload_for_ppt_extracts_structure_and_style() -> None:
    profiled = profile_reference_payload(
        format_name="pptx",
        inspect_payload={
            "outline": [{"title": "Intro"}, {"title": "Plan"}],
            "stats": {"slide_count": 2, "layout_variety_count": 2},
        },
    )

    assert profiled["structure"]["units"][0]["name"] == "Intro"
    assert profiled["style"]["style_tokens"]["slide_count"] == 2


def test_resolve_reference_constraints_keeps_goal_first() -> None:
    merged = resolve_reference_constraints(
        goal_constraints={"hard_requirements": ["rename summary sheet"]},
        reference_structure_constraints={"units": [{"name": "Summary"}]},
        reference_style_constraints={"style_tokens": {"theme": "blue"}},
        existing_document_profile={"protected_units": ["RawData"]},
    )

    assert merged["goal_constraints"]["hard_requirements"] == ["rename summary sheet"]
    assert merged["conflict_resolution"]["priority_order"] == ["goal", "reference"]
