from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

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


def test_officecli_command_input_rejects_invalid_view_without_mode() -> None:
    from agent.workflows.office.tools import OfficeCliCommandInput

    with pytest.raises(ValidationError):
        OfficeCliCommandInput.model_validate({"verb": "view", "file": "deck.pptx"})


def test_officecli_command_input_keeps_default_file_compatibility_for_view() -> None:
    from agent.workflows.office.tools import OfficeCliCommandInput

    with patch(
        "agent.workflows.office.tools.get_office_constraints",
        return_value={
            "runtime_target": "desktop",
            "allowed_output_dir": "/tmp/outputs",
            "allowed_source_files": [],
            "default_create_file": "deck.pptx",
        },
    ):
        payload = OfficeCliCommandInput.model_validate({"verb": "view", "mode": "outline"})

    assert payload.file == "deck.pptx"
    assert payload.mode == "outline"


def test_officecli_tool_schema_exports_discriminated_command_shapes() -> None:
    from agent.workflows.office.tools import officecli

    tool_schema = convert_to_openai_tool(officecli)
    parameters = tool_schema["function"]["parameters"]

    assert "oneOf" in parameters
    assert parameters["discriminator"]["propertyName"] == "verb"
    assert "required" not in parameters or parameters["required"] != ["verb"]


def test_officecli_batch_tool_schema_exports_discriminated_item_shapes() -> None:
    from agent.workflows.office.tools import officecli_batch

    tool_schema = convert_to_openai_tool(officecli_batch)
    parameters = tool_schema["function"]["parameters"]
    commands = parameters["properties"]["commands"]

    assert commands["type"] == "array"
    assert "oneOf" in commands["items"]
    assert commands["items"]["discriminator"]["propertyName"] == "verb"


def test_officecli_batch_schema_description_warns_against_stringified_commands() -> None:
    from agent.workflows.office.tools import officecli_batch

    tool_schema = convert_to_openai_tool(officecli_batch)
    description = tool_schema["function"]["parameters"]["properties"]["commands"]["description"]

    assert "native array" in description
    assert "not a JSON string" in description
    assert '"commands":[{"verb":"create","file":"demo.pptx"}]' in description
    assert '"commands":"[{' in description


def test_officecli_batch_strict_schema_experiment_exports_strict_flag() -> None:
    from agent.workflows.office.tools import officecli_batch

    tool_schema = convert_to_openai_tool(officecli_batch, strict=True)

    assert tool_schema["function"]["strict"] is True


def test_coerce_batch_commands_input_logs_malformed_json_context(caplog: pytest.LogCaptureFixture) -> None:
    from agent.workflows.office.tools import _coerce_batch_commands_input

    # Structurally damaged beyond repair: unterminated string.
    broken = '[{"verb":"add","file":"deck.pptx","parent":"/","type":"shape","props":{"text":"un'

    caplog.set_level(logging.ERROR, logger="chatdada.office.tools")

    with pytest.raises(json.JSONDecodeError):
        _coerce_batch_commands_input(broken)

    assert "officecli_batch received malformed commands JSON" in caplog.text
    assert "length=" in caplog.text
    assert "error_pos=" in caplog.text
    assert "preview=" in caplog.text


def test_coerce_batch_commands_input_repairs_missing_object_close_brace_between_commands() -> None:
    from agent.workflows.office.tools import _coerce_batch_commands_input

    # Reproduces the failure observed in production: the LLM closes `props` but
    # forgets to close the outer command object before starting the next peer,
    # producing `..."props":{...},{"verb":...` instead of `..."props":{...}},{"verb":...`.
    broken = (
        '[{"verb":"add","file":"deck.pptx","parent":"/","type":"shape","props":{"text":"ok"}},'
        '{"verb":"add","file":"deck.pptx","parent":"/slide[1]","type":"shape","props":{"text":"bad"},'
        '{"verb":"add","file":"deck.pptx","parent":"/slide[2]","type":"shape","props":{"text":"tail"}}]'
    )

    repaired = _coerce_batch_commands_input(broken)

    assert [item["parent"] for item in repaired] == ["/", "/slide[1]", "/slide[2]"]
    assert [item["props"]["text"] for item in repaired] == ["ok", "bad", "tail"]


def test_coerce_batch_commands_input_repairs_missing_brace_in_long_shape_batch() -> None:
    from agent.workflows.office.tools import _coerce_batch_commands_input

    # Matches the exact preview shape from the reported production trace:
    # multi-prop shape command where the LLM only emitted a single `}` after
    # `"line":"none"` before moving to the next command.
    broken = (
        '[{"verb":"add","file":"fishing-benefits-for-men.pptx","parent":"/slide[2]",'
        '"type":"shape","props":{"preset":"roundRect","fill":"1E2761","x":"1.5cm",'
        '"y":"3.5cm","width":"0.8cm","height":"0.8cm","line":"none"},'
        '{"verb":"add","file":"fishing-benefits-for-men.pptx","parent":"/slide[2]",'
        '"type":"shape","props":{"text":"01","x":"1.5cm","y":"3.5cm","width":"0.8cm"}}]'
    )

    repaired = _coerce_batch_commands_input(broken)

    assert len(repaired) == 2
    assert repaired[0]["props"]["line"] == "none"
    assert repaired[1]["props"]["text"] == "01"


def test_coerce_batch_commands_input_leaves_valid_json_untouched() -> None:
    from agent.workflows.office.tools import _coerce_batch_commands_input

    # Regression guard: the brace-repair scanner must not mutate commands that
    # already parse cleanly. Nested objects, arrays, and strings containing
    # `, {` must pass through unchanged.
    payload = json.dumps(
        [
            {
                "verb": "add",
                "file": "deck.pptx",
                "parent": "/",
                "type": "shape",
                "props": {"text": "example with , { inside"},
                "options": {"tags": ["a", "b"]},
            },
            {
                "verb": "set",
                "file": "deck.pptx",
                "path": "/slide[1]",
                "props": {"layout": "hero"},
            },
        ],
        ensure_ascii=False,
    )

    parsed = _coerce_batch_commands_input(payload)

    assert parsed[0]["props"]["text"] == "example with , { inside"
    assert parsed[1]["path"] == "/slide[1]"


def test_coerce_batch_commands_input_repairs_missing_props_object_brace() -> None:
    from agent.workflows.office.tools import _coerce_batch_commands_input

    broken = (
        '[{"verb":"add","file":"deck.pptx","parent":"/slide[1]","type":"shape",'
        '"props":"text":"hello","x":"1cm","y":"2cm"}}]'
    )

    repaired = _coerce_batch_commands_input(broken)

    assert repaired == [
        {
            "verb": "add",
            "file": "deck.pptx",
            "parent": "/slide[1]",
            "type": "shape",
            "props": {"text": "hello", "x": "1cm", "y": "2cm"},
        }
    ]


def test_coerce_batch_commands_input_repairs_unescaped_quotes_inside_text_value() -> None:
    from agent.workflows.office.tools import _coerce_batch_commands_input

    broken = (
        '[{"verb":"add","file":"deck.pptx","parent":"/slide[1]","type":"notes",'
        '"props":{"text":"长期钓鱼者往往反馈"像换了一个人"——不是夸张"}}]'
    )

    repaired = _coerce_batch_commands_input(broken)

    assert repaired[0]["props"]["text"] == '长期钓鱼者往往反馈"像换了一个人"——不是夸张'


def test_coerce_batch_commands_input_repairs_broken_key_value_delimiter() -> None:
    from agent.workflows.office.tools import _coerce_batch_commands_input

    broken = (
        '[{"verb":"add","file":"deck.pptx","parent":"/slide[1]","type":"shape","props":{"text":"ok"}},'
        '{"verb":="add","file":"deck.pptx","parent":"/slide[2]","type":"shape","props":{"text":"fixed"}}]'
    )

    repaired = _coerce_batch_commands_input(broken)

    assert repaired[1]["verb"] == "add"
    assert repaired[1]["parent"] == "/slide[2]"
    assert repaired[1]["props"]["text"] == "fixed"


@pytest.mark.asyncio
async def test_officecli_batch_accepts_json_string_commands_payload() -> None:
    from agent.workflows.office.tools import officecli_batch

    with patch(
        "agent.workflows.office.tools.execute_officecli_specs",
        new=AsyncMock(
            return_value={
                "success": True,
                "command": "officecli_batch",
                "exit_status": 0,
                "kind": "success",
                "message": "All commands completed successfully.",
                "results": [{"command": "officecli create demo.pptx"}],
                "runtime_target": "desktop",
                "artifacts": [],
            }
        ),
    ) as mocked_execute:
        result = await officecli_batch.ainvoke(
            {
                "commands": json.dumps(
                    [
                        {"verb": "create", "file": "demo.pptx"},
                    ],
                    ensure_ascii=False,
                )
            }
        )

    payload = json.loads(result)
    assert payload["success"] is True
    mocked_execute.assert_awaited_once_with([{"verb": "create", "file": "demo.pptx"}])
