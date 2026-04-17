"""Office domain OfficeCLI tools."""
from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field, model_validator

from agent.runtime.cost_logging import log_cost_record
from agent.tools.officecli import (
    SUPPORTED_FORMATS,
    SUPPORTED_VERBS,
    execute_officecli_raw,
    execute_officecli_spec,
    execute_officecli_specs,
)


class OfficeCliCommandInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, value: Any) -> Any:
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
        return normalized

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
        description="Structured OfficeCLI commands using the same `verb`-based fields as the officecli tool.",
    )


@tool("officecli", args_schema=OfficeCliCommandInput)
async def officecli(
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
    payload = await execute_officecli_spec(
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
    _log_office_tool_payload("officecli", payload)
    return json.dumps(payload, ensure_ascii=False)


@tool("officecli_batch", args_schema=OfficeCliBatchInput)
async def officecli_batch(commands: list[OfficeCliCommandInput]) -> str:
    """Execute multiple structured OfficeCLI commands and aggregate the results."""
    payload = await execute_officecli_specs([command.model_dump(exclude_none=True) for command in commands])
    _log_office_tool_payload("officecli_batch", payload)
    return json.dumps(payload, ensure_ascii=False)


@tool("officecli_run")
async def officecli_run(command: str) -> str:
    """Execute a single raw OfficeCLI command string for compatibility flows."""
    payload = await execute_officecli_raw(command)
    _log_office_tool_payload("officecli_run", payload)
    return json.dumps(payload, ensure_ascii=False)


def get_office_tools():
    """Return tools available to the Office domain."""
    return [officecli, officecli_batch]


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
    "SUPPORTED_FORMATS",
    "SUPPORTED_VERBS",
    "get_office_tools",
    "officecli",
    "officecli_batch",
    "officecli_run",
]
