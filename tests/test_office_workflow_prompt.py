from agent.workflows.office.workflow import _OFFICE_SYSTEM


def test_office_system_prompt_forbids_stringified_officecli_batch_commands() -> None:
    assert "officecli_batch" in _OFFICE_SYSTEM
    assert "commands must be a native array" in _OFFICE_SYSTEM
    assert "Do not stringify the array" in _OFFICE_SYSTEM


def test_office_system_prompt_formats_without_keyerror() -> None:
    rendered = _OFFICE_SYSTEM.format(
        format_hint="pptx",
        operation="create",
        runtime_target="desktop",
        default_create_file="deck.pptx",
        source_files_block="- 无",
        format_specific_guidance="",
        phase_guidance="",
        skill_content="",
    )

    assert '{"commands":[{"verb":"create","file":"deck.pptx"}]}' in rendered
