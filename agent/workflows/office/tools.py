"""Office domain OfficeCLI tools."""
from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Literal
from typing import TypeAlias

from langchain_core.tools import StructuredTool, tool
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from agent.runtime.cost_logging import log_cost_record
from agent.tools.officecli import (
    SUPPORTED_FORMATS,
    SUPPORTED_VERBS,
    execute_officecli_raw,
    execute_officecli_spec,
    execute_officecli_specs,
    get_office_constraints,
)

_log = logging.getLogger("chatdada.office.tools")
_BROKEN_NESTED_OBJECT_RE = re.compile(r'"(props|options)"\s*:\s*"([A-Za-z_][^"]*)"\s*:')
_BROKEN_KEY_VALUE_DELIMITER_RE = re.compile(r'("[A-Za-z_][^"]*")\s*:=\s*')

_FILE_VERBS = {
    "create",
    "open",
    "close",
    "view",
    "get",
    "query",
    "set",
    "add",
    "remove",
    "validate",
    "watch",
    "unwatch",
}


def _normalize_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_props(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items() if str(k).strip()}
    return {}


def _normalize_options(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items() if str(k).strip()}
    return {}


def _derive_default_file(constraints: dict[str, Any], *, verb: str) -> str | None:
    if verb not in _FILE_VERBS:
        return None

    default_create_file = _normalize_optional_string(constraints.get("default_create_file"))
    if default_create_file is not None:
        return default_create_file

    raw_sources = constraints.get("allowed_source_files")
    if isinstance(raw_sources, list):
        candidates = [_normalize_optional_string(item) for item in raw_sources]
        unique_candidates = [item for item in candidates if item]
        if len(unique_candidates) == 1:
            return unique_candidates[0]

    return None


def _normalize_legacy_officecli_payload(
    value: Any,
    *,
    constraints: dict[str, Any] | None = None,
) -> Any:
    if not isinstance(value, dict):
        return value

    normalized = dict(value)
    if "verb" not in normalized:
        legacy_verb = normalized.pop("command", None)
        if legacy_verb is None:
            legacy_verb = normalized.pop("operation", None)
        if legacy_verb is not None:
            normalized["verb"] = legacy_verb
    if "props" not in normalized and "properties" in normalized:
        normalized["props"] = normalized.pop("properties")

    verb = str(normalized.get("verb") or "").strip().lower()
    if verb:
        normalized["verb"] = verb

    active_constraints = dict(constraints or get_office_constraints())
    current_file = _normalize_optional_string(normalized.get("file"))
    if current_file is None:
        default_file = _derive_default_file(active_constraints, verb=verb)
        if default_file is not None:
            normalized["file"] = default_file
    else:
        normalized["file"] = current_file

    normalized["format"] = _normalize_optional_string(normalized.get("format"))
    normalized["path"] = _normalize_optional_string(normalized.get("path"))
    normalized["parent"] = _normalize_optional_string(normalized.get("parent"))
    normalized["type"] = _normalize_optional_string(normalized.get("type"))
    normalized["mode"] = _normalize_optional_string(normalized.get("mode"))
    normalized["selector"] = _normalize_optional_string(normalized.get("selector"))
    normalized["props"] = _normalize_props(normalized.get("props"))
    normalized["options"] = _normalize_options(normalized.get("options"))

    if verb == "help" and not normalized.get("format"):
        option_format = _normalize_optional_string(normalized["options"].get("format"))
        if option_format is not None:
            normalized["format"] = option_format

    return normalized


class _OfficeCliCanonicalBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CreateCommand(_OfficeCliCanonicalBase):
    verb: Literal["create"]
    file: str


class OpenLikeCommand(_OfficeCliCanonicalBase):
    verb: Literal["open", "close", "validate", "watch", "unwatch"]
    file: str
    options: dict[str, Any] | None = None


class ViewCommand(_OfficeCliCanonicalBase):
    verb: Literal["view"]
    file: str
    mode: str
    options: dict[str, Any] | None = None


class GetCommand(_OfficeCliCanonicalBase):
    verb: Literal["get"]
    file: str
    path: str
    options: dict[str, Any] | None = None


class QueryCommand(_OfficeCliCanonicalBase):
    verb: Literal["query"]
    file: str
    selector: str
    options: dict[str, Any] | None = None


class SetCommand(_OfficeCliCanonicalBase):
    verb: Literal["set"]
    file: str
    path: str
    props: dict[str, Any]
    options: dict[str, Any] | None = None


class AddCommand(_OfficeCliCanonicalBase):
    verb: Literal["add"]
    file: str
    parent: str
    type: str
    props: dict[str, Any] | None = None
    options: dict[str, Any] | None = None


class RemoveCommand(_OfficeCliCanonicalBase):
    verb: Literal["remove"]
    file: str
    path: str
    options: dict[str, Any] | None = None


class HelpCommand(_OfficeCliCanonicalBase):
    verb: Literal["help"]
    format: Literal["pptx", "docx", "xlsx"]
    options: dict[str, Any] | None = None


OfficeCliCanonicalCommand: TypeAlias = Annotated[
    CreateCommand
    | OpenLikeCommand
    | ViewCommand
    | GetCommand
    | QueryCommand
    | SetCommand
    | AddCommand
    | RemoveCommand
    | HelpCommand,
    Field(discriminator="verb"),
]


_OFFICECLI_COMMAND_ADAPTER = TypeAdapter(OfficeCliCanonicalCommand)
_OFFICECLI_BATCH_COMMANDS_DESCRIPTION = (
    "Structured OfficeCLI commands using the same `verb`-based fields as the officecli tool. "
    "For `officecli_batch`, `commands` must be a native array of command objects, not a JSON string. "
    'Good: {"commands":[{"verb":"create","file":"demo.pptx"}]}. '
    'Bad: {"commands":"[{\\"verb\\":\\"create\\",\\"file\\":\\"demo.pptx\\"}]"}'
)


def _canonical_command_schema(*args: Any, **kwargs: Any) -> dict[str, Any]:
    schema = _OFFICECLI_COMMAND_ADAPTER.json_schema(*args, **kwargs)
    if isinstance(schema, dict):
        schema = dict(schema)
        schema["title"] = "OfficeCliCommandInput"
    return schema


def _canonical_batch_schema(*args: Any, **kwargs: Any) -> dict[str, Any]:
    item_schema = _canonical_command_schema(*args, **kwargs)
    schema = {
        "title": "OfficeCliBatchInput",
        "type": "object",
        "properties": {
            "commands": {
                "type": "array",
                "description": _OFFICECLI_BATCH_COMMANDS_DESCRIPTION,
                "items": item_schema,
            }
        },
        "required": ["commands"],
    }
    defs = item_schema.get("$defs")
    if isinstance(defs, dict) and defs:
        schema["$defs"] = defs
    return schema


def canonicalize_officecli_command_payload(
    payload: dict[str, Any],
    *,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_legacy_officecli_payload(payload, constraints=constraints)
    command = _OFFICECLI_COMMAND_ADAPTER.validate_python(normalized)
    return command.model_dump(exclude_none=True)


def _json_error_preview(text: str, *, position: int, radius: int = 160) -> str:
    start = max(0, position - radius)
    end = min(len(text), position + radius)
    excerpt = text[start:end]
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt = excerpt + "..."
    return excerpt


def _escape_interior_quotes_in_json_strings(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False

    i = 0
    while i < len(text):
        ch = text[i]
        if not in_string:
            if ch == '"':
                in_string = True
            result.append(ch)
            i += 1
            continue

        if escaped:
            result.append(ch)
            escaped = False
            i += 1
            continue

        if ch == "\\":
            result.append(ch)
            escaped = True
            i += 1
            continue

        if ch == '"':
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j >= len(text) or text[j] in {",", "}", "]", ":"}:
                result.append(ch)
                in_string = False
            else:
                result.append('\\"')
            i += 1
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def _repair_missing_object_close_braces(text: str) -> str:
    """Insert a missing `}` before ``, {`` when it appears inside an object.

    LLMs occasionally emit a batch like ``[{..., "props":{...},{"verb":...}}]``
    where the outer command object's closing brace is dropped between peers.
    A ``{`` can never legally follow ``,`` inside a JSON object (keys must be
    strings), so whenever the scanner sees that pattern it closes the current
    object before the comma. Valid JSON is left untouched because the pattern
    is structurally impossible there.
    """
    result: list[str] = []
    container_stack: list[str] = []
    in_string = False
    escaped = False

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            result.append(ch)
            i += 1
            continue

        if ch in "[{":
            container_stack.append(ch)
            result.append(ch)
            i += 1
            continue

        if ch in "]}":
            if container_stack:
                container_stack.pop()
            result.append(ch)
            i += 1
            continue

        if ch == ",":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == "{" and container_stack and container_stack[-1] == "{":
                result.append("}")
                container_stack.pop()
            result.append(ch)
            i += 1
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def _repair_stringified_batch_commands_json(text: str) -> str:
    repaired = _BROKEN_KEY_VALUE_DELIMITER_RE.sub(r"\1: ", text)
    repaired = _BROKEN_NESTED_OBJECT_RE.sub(r'"\1":{"\2":', repaired)
    repaired = _escape_interior_quotes_in_json_strings(repaired)
    repaired = _repair_missing_object_close_braces(repaired)
    return repaired


def _coerce_batch_commands_input(
    commands: Any,
) -> list[dict[str, Any] | OfficeCliCommandInput]:
    if isinstance(commands, str):
        try:
            parsed = json.loads(commands)
        except json.JSONDecodeError as exc:
            _log.error(
                "officecli_batch received malformed commands JSON: length=%s error_pos=%s preview=%r",
                len(commands),
                exc.pos,
                _json_error_preview(commands, position=exc.pos),
            )
            repaired = _repair_stringified_batch_commands_json(commands)
            if repaired != commands:
                parsed = json.loads(repaired)
                _log.warning(
                    "officecli_batch repaired malformed commands JSON: original_error_pos=%s preview=%r",
                    exc.pos,
                    _json_error_preview(repaired, position=min(exc.pos, max(len(repaired) - 1, 0))),
                )
            else:
                raise
        if isinstance(parsed, dict) and "commands" in parsed:
            parsed = parsed["commands"]
        commands = parsed

    if not isinstance(commands, list):
        raise ValueError("officecli_batch requires commands to be a list or JSON array string.")

    return list(commands)


class OfficeCliCommandInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, value: Any) -> Any:
        return _normalize_legacy_officecli_payload(value)

    @model_validator(mode="after")
    def _validate_canonical_shape(self) -> "OfficeCliCommandInput":
        canonicalize_officecli_command_payload(self.model_dump(mode="python", exclude_none=False))
        return self

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return _canonical_command_schema(*args, **kwargs)

    verb: Literal[
        "create",
        "open",
        "close",
        "view",
        "get",
        "query",
        "set",
        "add",
        "remove",
        "validate",
        "help",
        "watch",
        "unwatch",
    ] = Field(description="Canonical OfficeCLI verb. Use `verb` in new calls.")
    file: str | None = Field(default=None, description="Office document path or filename.")
    format: Literal["pptx", "docx", "xlsx"] | None = Field(
        default=None,
        description="Document format for help or format-scoped commands.",
    )
    path: str | None = Field(default=None, description="Target DOM path for get/set/remove.")
    parent: str | None = Field(default=None, description="Parent DOM path for add.")
    type: str | None = Field(default=None, description="Element type for add.")
    props: dict[str, Any] | None = Field(default=None, description="Repeated --prop key=value pairs.")
    mode: str | None = Field(default=None, description="View mode for `view`.")
    selector: str | None = Field(default=None, description="Selector or query expression for `query`.")
    options: dict[str, Any] | None = Field(default=None, description="Additional official OfficeCLI options.")


class OfficeCliBatchInput(BaseModel):
    commands: list[OfficeCliCommandInput] = Field(
        description=_OFFICECLI_BATCH_COMMANDS_DESCRIPTION,
    )

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return _canonical_batch_schema(*args, **kwargs)


async def _officecli_impl(
    verb: str,
    file: str | None = None,
    format: str | None = None,
    path: str | None = None,
    parent: str | None = None,
    type: str | None = None,
    props: dict[str, Any] | None = None,
    mode: str | None = None,
    selector: str | None = None,
    options: dict[str, Any] | None = None,
) -> str:
    """Execute a structured OfficeCLI command aligned with official CLI verbs."""
    command = canonicalize_officecli_command_payload(
        {
            "verb": verb,
            "file": file,
            "format": format,
            "path": path,
            "parent": parent,
            "type": type,
            "props": props,
            "mode": mode,
            "selector": selector,
            "options": options,
        }
    )
    payload = await execute_officecli_spec(command)
    _log_office_tool_payload("officecli", payload)
    return json.dumps(payload, ensure_ascii=False)


async def _officecli_batch_impl(commands: list[dict[str, Any] | OfficeCliCommandInput]) -> str:
    """Execute multiple structured OfficeCLI commands and aggregate the results."""
    commands = _coerce_batch_commands_input(commands)
    canonical_commands = [
        canonicalize_officecli_command_payload(
            command.model_dump(mode="python", exclude_none=False)
            if isinstance(command, OfficeCliCommandInput)
            else dict(command)
        )
        for command in commands
    ]
    payload = await execute_officecli_specs(canonical_commands)
    _log_office_tool_payload("officecli_batch", payload)
    return json.dumps(payload, ensure_ascii=False)


officecli = StructuredTool.from_function(
    coroutine=_officecli_impl,
    name="officecli",
    description="Execute a structured OfficeCLI command aligned with official CLI verbs.",
    args_schema=_canonical_command_schema(),
    infer_schema=False,
)


officecli_batch = StructuredTool.from_function(
    coroutine=_officecli_batch_impl,
    name="officecli_batch",
    description="Execute multiple structured OfficeCLI commands and aggregate the results.",
    args_schema=_canonical_batch_schema(),
    infer_schema=False,
)


@tool("officecli_run")
async def officecli_run(command: str) -> str:
    """Execute a single raw OfficeCLI command string for compatibility flows."""
    payload = await execute_officecli_raw(command)
    _log_office_tool_payload("officecli_run", payload)
    return json.dumps(payload, ensure_ascii=False)


def get_office_tools():
    """Return tools available to the Office domain."""
    # 引入图像工具：image_gen（文生图）+ list_local_images（扫描本地素材）
    # 让 office workflow 自带配图能力，可与 officecli add --type picture 串联。
    from agent.workflows.office.image_tools import get_office_image_tools

    return [officecli, officecli_batch, *get_office_image_tools()]


def _log_office_tool_payload(tool_name: str, payload: dict[str, Any]) -> None:
    try:
        from langgraph.config import get_config

        configurable = get_config().get("configurable", {}) or {}
    except Exception:
        configurable = {}

    task_id = str(configurable.get("thread_id", "") or configurable.get("task_id", "") or "office_unknown")
    stage = str(configurable.get("office_cost_stage", "") or "build")
    log_cost_record(
        "call",
        {
            "task_id": task_id,
            "domain": "office",
            "stage": stage,
            "call_type": "tool",
            "name": tool_name,
            "estimated_cost_usd": 0.0,
            "execution_time_ms": int(payload.get("execution_time_ms", 0) or 0),
            "result_kind": str(payload.get("kind", "") or ""),
            "command": str(payload.get("command", "") or ""),
            "message": str(payload.get("message", "") or ""),
        },
    )


__all__ = [
    "OfficeCliBatchInput",
    "OfficeCliCommandInput",
    "canonicalize_officecli_command_payload",
    "SUPPORTED_FORMATS",
    "SUPPORTED_VERBS",
    "get_office_tools",
    "officecli",
    "officecli_batch",
    "officecli_run",
]
