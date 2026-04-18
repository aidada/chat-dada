import asyncio
import json

from agent.domains.office import reference_inspector
from agent.domains.office.reference_profiler import profile_reference_payload
from agent.domains.office.reference_resolver import resolve_reference_constraints


def test_inspect_reference_file_unwraps_officecli_envelopes_into_payloads(monkeypatch) -> None:
    envelopes = iter(
        [
            {
                "success": True,
                "kind": "success",
                "exit_status": 0,
                "message": "outline ready",
                "raw_stdout": json.dumps([{"title": "Intro"}, {"title": "Plan"}]),
                "raw_stderr": "",
            },
            {
                "success": True,
                "kind": "success",
                "exit_status": 0,
                "message": "stats ready",
                "raw_stdout": json.dumps({"slide_count": 2, "layout_variety_count": 2}),
                "raw_stderr": "",
            },
        ]
    )

    async def fake_execute_officecli_spec(spec):
        return next(envelopes)

    monkeypatch.setattr(reference_inspector, "execute_officecli_spec", fake_execute_officecli_spec)

    profiled = asyncio.run(reference_inspector.inspect_reference_file(format_name="pptx", file_path="deck.pptx"))

    assert profiled["outline"] == [{"title": "Intro"}, {"title": "Plan"}]
    assert profiled["stats"] == {"slide_count": 2, "layout_variety_count": 2}


def test_inspect_reference_file_canonicalizes_uppercase_spaced_format_name(monkeypatch) -> None:
    async def fake_execute_officecli_spec(spec):
        if spec["mode"] == "text":
            return {
                "success": True,
                "kind": "success",
                "exit_status": 0,
                "message": "text ready",
                "raw_stdout": json.dumps("sheet text"),
                "raw_stderr": "",
            }
        return {
            "success": True,
            "kind": "success",
            "exit_status": 0,
            "message": "issues ready",
            "raw_stdout": json.dumps({"text": "sheet issues"}),
            "raw_stderr": "",
        }

    monkeypatch.setattr(reference_inspector, "execute_officecli_spec", fake_execute_officecli_spec)

    profiled = asyncio.run(reference_inspector.inspect_reference_file(format_name=" XLSX ", file_path="book.xlsx"))

    assert profiled["text"] == "sheet text"
    assert profiled["issues"]["text"] == "sheet issues"
    assert profiled["issues"]["message"] == "issues ready"


def test_inspect_reference_file_falls_back_when_json_top_level_type_is_wrong(monkeypatch) -> None:
    async def fake_execute_officecli_spec(spec):
        if spec["mode"] == "outline":
            return {
                "success": True,
                "kind": "success",
                "exit_status": 0,
                "message": "outline ready",
                "raw_stdout": json.dumps({"title": "Intro"}),
                "raw_stderr": "",
            }
        return {
            "success": True,
            "kind": "success",
            "exit_status": 0,
            "message": "stats ready",
            "raw_stdout": json.dumps(["unexpected"]),
            "raw_stderr": "",
        }

    monkeypatch.setattr(reference_inspector, "execute_officecli_spec", fake_execute_officecli_spec)

    profiled = asyncio.run(reference_inspector.inspect_reference_file(format_name="pptx", file_path="deck.pptx"))

    assert profiled["outline"] == []
    assert profiled["stats"]["slide_count"] == 0
    assert profiled["stats"]["layout_variety_count"] == 0


def test_profile_reference_payload_for_ppt_extracts_structure_and_style() -> None:
    profiled = profile_reference_payload(
        format_name=" PPTX ",
        inspect_payload={
            "outline": [{"title": "Intro"}, {"title": "   "}, {"title": "Plan"}],
            "stats": {"slide_count": 2, "layout_variety_count": 2},
        },
    )

    assert profiled["structure"]["format"] == "pptx"
    assert profiled["style"]["format"] == "pptx"
    assert profiled["structure"]["units"] == [{"name": "Intro"}, {"name": "Plan"}]
    assert profiled["style"]["style_tokens"]["slide_count"] == 2


def test_profile_reference_payload_for_non_ppt_defaults_to_empty_constraints() -> None:
    profiled = profile_reference_payload(format_name="docx", inspect_payload={"unexpected": True})

    assert profiled["structure"]["units"] == []
    assert profiled["style"]["style_tokens"] == {}


def test_profile_reference_payload_for_xlsx_extracts_minimal_structure_from_text_and_issues() -> None:
    profiled = profile_reference_payload(
        format_name="xlsx",
        inspect_payload={
            "text": "Workbook sheets: Summary, RawData, Dashboard",
            "issues": {
                "text": "Check formulas on Summary sheet and Dashboard chart titles.",
                "message": "sheet scan complete",
            },
        },
    )

    assert profiled["structure"]["format"] == "xlsx"
    assert profiled["style"]["format"] == "xlsx"
    assert profiled["structure"]["units"] == [
        {"name": "Summary"},
        {"name": "RawData"},
        {"name": "Dashboard"},
    ]
    assert profiled["style"]["style_tokens"]["issue_summary"] == "sheet scan complete"


def test_profile_reference_payload_for_docx_extracts_minimal_structure_and_style() -> None:
    profiled = profile_reference_payload(
        format_name="docx",
        inspect_payload={
            "text": "Document sections: Executive Summary\nImplementation Plan\nAppendix",
            "annotated": {
                "text": "Uses Heading 1 for section titles and a formal tone.",
                "message": "annotation complete",
            },
        },
    )

    assert profiled["structure"]["format"] == "docx"
    assert profiled["style"]["format"] == "docx"
    assert profiled["structure"]["units"] == [
        {"name": "Executive Summary"},
        {"name": "Implementation Plan"},
        {"name": "Appendix"},
    ]
    assert profiled["style"]["style_tokens"]["annotation_summary"] == "annotation complete"


def test_resolve_reference_constraints_keeps_goal_first() -> None:
    merged = resolve_reference_constraints(
        goal_constraints={"hard_requirements": ["rename summary sheet"]},
        reference_structure_constraints={"units": [{"name": "Summary"}]},
        reference_style_constraints={"style_tokens": {"theme": "blue"}},
        existing_document_profile={"protected_units": ["RawData"]},
    )

    assert merged["goal_constraints"]["hard_requirements"] == ["rename summary sheet"]
    assert merged["conflict_resolution"]["priority_order"] == ["goal", "reference"]


def test_resolve_reference_constraints_deep_copies_nested_inputs() -> None:
    structure_constraints = {"units": [{"name": "Summary"}]}
    style_constraints = {"style_tokens": {"theme": "blue"}}
    document_profile = {"protected_units": ["RawData"]}

    merged = resolve_reference_constraints(
        goal_constraints={"hard_requirements": ["rename summary sheet"]},
        reference_structure_constraints=structure_constraints,
        reference_style_constraints=style_constraints,
        existing_document_profile=document_profile,
    )

    structure_constraints["units"][0]["name"] = "Changed"
    style_constraints["style_tokens"]["theme"] = "green"
    document_profile["protected_units"][0] = "Changed"

    assert merged["reference_structure_constraints"]["units"][0]["name"] == "Summary"
    assert merged["reference_style_constraints"]["style_tokens"]["theme"] == "blue"
    assert merged["existing_document_profile"]["protected_units"] == ["RawData"]


def test_resolve_reference_constraints_normalizes_required_fields() -> None:
    merged = resolve_reference_constraints(
        goal_constraints={"format": " PPTX ", "hard_requirements": ["rename summary sheet"]},
        reference_structure_constraints={"format": " PPTX ", "units": [{"name": "Summary"}]},
        reference_style_constraints={"format": " PPTX ", "style_tokens": {"theme": "blue"}},
        existing_document_profile={"format": " DOCX ", "protected_units": ["RawData"]},
    )

    assert merged["goal_constraints"] == {
        "format": "pptx",
        "operation": "",
        "goal": "",
        "hard_requirements": ["rename summary sheet"],
        "section_headings": [],
        "formatting_instructions": [],
    }
    assert merged["reference_structure_constraints"]["format"] == "pptx"
    assert merged["reference_structure_constraints"]["units"] == [{"name": "Summary"}]
    assert merged["reference_style_constraints"]["format"] == "pptx"
    assert merged["reference_style_constraints"]["style_tokens"] == {"theme": "blue"}
    assert merged["existing_document_profile"]["format"] == "docx"
    assert merged["existing_document_profile"]["units"] == []
    assert merged["existing_document_profile"]["protected_units"] == ["RawData"]


def test_resolve_reference_constraints_uses_style_format_for_goal_fallback() -> None:
    merged = resolve_reference_constraints(
        goal_constraints={"hard_requirements": ["rename summary sheet"]},
        reference_structure_constraints={"units": [{"name": "Summary"}]},
        reference_style_constraints={"format": "pptx", "style_tokens": {"theme": "blue"}},
        existing_document_profile={"protected_units": ["RawData"]},
    )

    assert merged["goal_constraints"]["format"] == "pptx"


def test_resolve_reference_constraints_does_not_derive_docx_headings_and_formatting_instructions() -> None:
    merged = resolve_reference_constraints(
        goal_constraints={
            "format": "docx",
            "hard_requirements": ["背景", "目标", "preserve formatting", "使用 Heading1"],
        },
        reference_structure_constraints={},
        reference_style_constraints={},
        existing_document_profile={},
    )

    assert merged["goal_constraints"]["hard_requirements"] == ["背景", "目标", "preserve formatting", "使用 Heading1"]
    assert merged["goal_constraints"]["section_headings"] == []
    assert merged["goal_constraints"]["formatting_instructions"] == []
