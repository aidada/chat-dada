from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.officecli import compile_officecli_argv


def test_compile_officecli_create_command() -> None:
    args = compile_officecli_argv({"verb": "create", "file": "demo.pptx"})
    assert args == ["create", "demo.pptx"]


def test_compile_officecli_add_command() -> None:
    args = compile_officecli_argv(
        {
            "verb": "add",
            "file": "demo.pptx",
            "parent": "/",
            "type": "slide",
            "props": {"title": "Q4 Report"},
            "options": {"index": 0},
        }
    )
    assert args == [
        "add",
        "demo.pptx",
        "/",
        "--type",
        "slide",
        "--prop",
        "title=Q4 Report",
        "--index",
        "0",
    ]


def test_compile_officecli_set_command() -> None:
    args = compile_officecli_argv(
        {
            "verb": "set",
            "file": "demo.pptx",
            "path": "/slide[1]/shape[1]",
            "props": {"text": "Hello", "bold": True},
        }
    )
    assert args == [
        "set",
        "demo.pptx",
        "/slide[1]/shape[1]",
        "--prop",
        "text=Hello",
        "--prop",
        "bold=true",
    ]


def test_compile_officecli_help_command() -> None:
    args = compile_officecli_argv(
        {
            "verb": "help",
            "format": "pptx",
            "options": {"topic": ["set", "shape"]},
        }
    )
    assert args == ["pptx", "set", "shape"]


def test_compile_officecli_normalizes_command_alias() -> None:
    args = compile_officecli_argv({"command": "create", "file": "demo.pptx"})
    assert args == ["create", "demo.pptx"]


def test_compile_officecli_create_uses_default_filename_from_constraints() -> None:
    args = compile_officecli_argv(
        {"verb": "create"},
        constraints={
            "runtime_target": "desktop",
            "allowed_output_dir": "/tmp/outputs",
            "allowed_source_files": [],
            "default_create_file": "chat-dada-intro.pptx",
        },
    )
    assert args == ["create", "chat-dada-intro.pptx"]


def test_compile_officecli_close_uses_default_filename_from_constraints() -> None:
    args = compile_officecli_argv(
        {"verb": "close"},
        constraints={
            "runtime_target": "desktop",
            "allowed_output_dir": "/tmp/outputs",
            "allowed_source_files": [],
            "default_create_file": "chat-dada-intro.pptx",
        },
    )
    assert args == ["close", "chat-dada-intro.pptx"]


def test_compile_officecli_rejects_unauthorized_source_file() -> None:
    with pytest.raises(ValueError):
        compile_officecli_argv(
            {
                "verb": "open",
                "file": "/tmp/blocked.docx",
            },
            constraints={
                "runtime_target": "server",
                "allowed_output_dir": "/tmp/outputs",
                "allowed_source_files": ["/tmp/allowed.docx"],
            },
        )


@pytest.mark.asyncio
async def test_execute_officecli_spec_preserves_gateway_runtime_metadata() -> None:
    from agent.tools.officecli import execute_officecli_spec

    with patch(
        "agent.tools.officecli._execute_officecli_spec_via_gateway",
        new=AsyncMock(
            return_value={
                "success": True,
                "stdout": "Validation passed",
                "stderr": "",
                "exit_status": 0,
                "runtime_target": "desktop",
                "artifacts": [{"name": "deck.pptx", "path": "/Users/test/Desktop/deck.pptx"}],
            }
        ),
    ):
        payload = await execute_officecli_spec({"verb": "validate", "file": "deck.pptx"})

    assert payload["runtime_target"] == "desktop"
    assert payload["artifacts"][0]["path"] == "/Users/test/Desktop/deck.pptx"


@pytest.mark.asyncio
async def test_execute_officecli_spec_does_not_revalidate_desktop_absolute_path() -> None:
    from agent.tools.officecli import execute_officecli_spec

    with (
        patch(
            "agent.tools.officecli._execute_officecli_spec_via_gateway",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "stdout": "Added slide at /slide[1]",
                    "stderr": "",
                    "exit_status": 0,
                    "runtime_target": "desktop",
                    "artifacts": [],
                }
            ),
        ),
        patch(
            "agent.tools.officecli.get_office_constraints",
            return_value={
                "runtime_target": "desktop",
                "allowed_output_dir": "/tmp/outputs",
                "allowed_source_files": [],
                "default_create_file": "",
            },
        ),
    ):
        payload = await execute_officecli_spec(
            {
                "verb": "add",
                "file": "/Users/test/Downloads/deck.pptx",
                "parent": "/",
                "type": "slide",
                "props": {"title": "Intro"},
            }
        )

    assert payload["success"] is True
    assert payload["command"] == "officecli add /Users/test/Downloads/deck.pptx / --type slide --prop title=Intro"


@pytest.mark.asyncio
async def test_execute_officecli_spec_uses_default_filename_for_create() -> None:
    from agent.tools.officecli import execute_officecli_spec

    captured_specs: list[dict[str, object]] = []

    async def fake_gateway(spec: dict[str, object]) -> dict[str, object]:
        captured_specs.append(spec)
        return {
            "success": True,
            "stdout": "Created chat-dada-intro.pptx",
            "stderr": "",
            "exit_status": 0,
            "runtime_target": "desktop",
            "artifacts": [],
        }

    with (
        patch(
            "agent.tools.officecli._execute_officecli_spec_via_gateway",
            new=AsyncMock(side_effect=fake_gateway),
        ),
        patch(
            "agent.tools.officecli.get_office_constraints",
            return_value={
                "runtime_target": "desktop",
                "allowed_output_dir": "/tmp/outputs",
                "allowed_source_files": [],
                "default_create_file": "chat-dada-intro.pptx",
            },
        ),
    ):
        payload = await execute_officecli_spec({"verb": "create", "file": None})

    assert payload["success"] is True
    assert captured_specs == [{"verb": "create", "file": "chat-dada-intro.pptx"}]


@pytest.mark.asyncio
async def test_execute_officecli_spec_uses_default_filename_for_close() -> None:
    from agent.tools.officecli import execute_officecli_spec

    captured_specs: list[dict[str, object]] = []

    async def fake_gateway(spec: dict[str, object]) -> dict[str, object]:
        captured_specs.append(spec)
        return {
            "success": True,
            "stdout": "Closing resident.",
            "stderr": "",
            "exit_status": 0,
            "runtime_target": "desktop",
            "artifacts": [],
        }

    with (
        patch(
            "agent.tools.officecli._execute_officecli_spec_via_gateway",
            new=AsyncMock(side_effect=fake_gateway),
        ),
        patch(
            "agent.tools.officecli.get_office_constraints",
            return_value={
                "runtime_target": "desktop",
                "allowed_output_dir": "/tmp/outputs",
                "allowed_source_files": [],
                "default_create_file": "chat-dada-intro.pptx",
            },
        ),
    ):
        payload = await execute_officecli_spec({"verb": "close", "file": None})

    assert payload["success"] is True
    assert captured_specs == [{"verb": "close", "file": "chat-dada-intro.pptx"}]


@pytest.mark.asyncio
async def test_execute_officecli_spec_classifies_desktop_timeout_as_fatal() -> None:
    from agent.tools.officecli import execute_officecli_spec

    with patch(
        "agent.tools.officecli._execute_officecli_spec_via_gateway",
        new=AsyncMock(
            return_value={
                "success": False,
                "stdout": "",
                "stderr": "Desktop tool call timeout after 30.0s",
                "exit_status": 1,
                "runtime_target": "desktop",
                "artifacts": [],
            }
        ),
    ):
        payload = await execute_officecli_spec({"verb": "create", "file": "deck.pptx"})

    assert payload["success"] is False
    assert payload["kind"] == "fatal_error"
    assert "timeout" in payload["message"].lower()


@pytest.mark.asyncio
async def test_execute_officecli_spec_classifies_found_validation_errors_as_failed() -> None:
    from agent.tools.officecli import execute_officecli_spec

    with patch(
        "agent.tools.officecli._execute_officecli_spec_via_gateway",
        new=AsyncMock(
            return_value={
                "success": True,
                "stdout": "Found 20 validation error(s):",
                "stderr": "",
                "exit_status": 0,
                "runtime_target": "desktop",
                "artifacts": [],
            }
        ),
    ):
        payload = await execute_officecli_spec({"verb": "validate", "file": "deck.pptx"})

    assert payload["success"] is False
    assert payload["kind"] == "validation_failed"
    assert "20" in payload["message"]


@pytest.mark.asyncio
async def test_execute_officecli_spec_blocks_repeated_fatal_command() -> None:
    from agent.tools.officecli import execute_officecli_spec

    with (
        patch(
            "agent.tools.officecli._execute_officecli_spec_via_gateway",
            new=AsyncMock(
                return_value={
                    "success": False,
                    "stdout": "",
                    "stderr": 'OfficeCLI view requires a non-empty "mode" parameter.',
                    "exit_status": 1,
                    "runtime_target": "desktop",
                    "artifacts": [],
                }
            ),
        ),
        patch(
            "agent.tools.officecli.get_office_constraints",
            return_value={
                "runtime_target": "desktop",
                "allowed_output_dir": "/tmp/outputs",
                "allowed_source_files": [],
                "default_create_file": "deck.pptx",
            },
        ),
        patch(
            "agent.tools.officecli._get_configurable",
            return_value={"thread_id": "task_repeat_fatal"},
        ),
    ):
        first = await execute_officecli_spec({"verb": "view", "file": None, "mode": None})
        second = await execute_officecli_spec({"verb": "view", "file": None, "mode": None})

    assert first["kind"] == "fatal_error"
    assert second["kind"] == "fatal_error"
    assert "Repeated fatal OfficeCLI command blocked" in second["message"]


@pytest.mark.asyncio
async def test_execute_officecli_specs_normalizes_command_alias_in_batch() -> None:
    from agent.tools.officecli import execute_officecli_specs

    with patch(
        "agent.tools.officecli._execute_officecli_spec_via_gateway",
        new=AsyncMock(
            return_value={
                "success": True,
                "stdout": "Created demo.pptx",
                "stderr": "",
                "exit_status": 0,
                "runtime_target": "desktop",
                "artifacts": [],
            }
        ),
    ):
        payload = await execute_officecli_specs([{"command": "create", "file": "demo.pptx"}])

    assert payload["success"] is True
    assert payload["results"][0]["command"] == "officecli create demo.pptx"


def test_officecli_batch_schema_normalizes_command_alias() -> None:
    from agent.workflows.office.tools import OfficeCliBatchInput

    payload = OfficeCliBatchInput.model_validate(
        {"commands": [{"command": "create", "file": "demo.pptx"}]}
    )

    assert payload.commands[0].verb == "create"
