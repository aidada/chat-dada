from __future__ import annotations

import json
import re
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from agent.hands.gateway import ToolGateway
from agent.hands.protocol import ToolCall, ToolContext, ToolResult


def format_desktop_capability_summary(descriptors: list[dict[str, Any]]) -> str:
    if not descriptors:
        return "当前没有可用的桌面本机工具连接。"

    lines = [
        "当前存在可用的桌面本机工具。若用户请求查看本地文件、系统信息、剪贴板、截图或执行本机操作，应优先使用这些工具，而不是直接声称无法访问本地环境。",
        "工具权限说明：safe 可直接执行，cautious 首次执行需用户确认，dangerous 每次执行都需用户确认。",
        "优先使用专用安全工具完成任务。查看目录用 list_dir，读取文件用 file_read，查找文件用 file_search，搜索文本用 grep；只有在专用工具无法满足需求时才考虑 shell。",
        "对 shell、file_delete、mouse、keyboard 这类高风险工具要特别克制；不要为了列目录、读文件、搜索文本这类只读任务去调用 shell。",
        "",
        "可用桌面工具：",
    ]
    for descriptor in descriptors:
        name = str(descriptor.get("name", "") or "").strip()
        if not name:
            continue
        description = str(descriptor.get("description", "") or "无描述").strip()
        permission = str(descriptor.get("permission_level", "") or "mixed")
        lines.append(f"- {name} [{permission}]：{description}")
        operations = descriptor.get("operations")
        if isinstance(operations, list) and operations:
            op_summary = ", ".join(
                f"{str(item.get('name', '') or '').strip()}:{str(item.get('permission_level', '') or 'safe').strip()}"
                for item in operations
                if isinstance(item, dict) and str(item.get("name", "") or "").strip()
            )
            if op_summary:
                lines.append(f"  operations: {op_summary}")
    return "\n".join(lines).strip()


def build_desktop_langchain_tools(
    descriptors: list[dict[str, Any]],
    gateway: ToolGateway,
    ctx: ToolContext,
) -> list[StructuredTool]:
    tools: list[StructuredTool] = []
    for descriptor in descriptors:
        name = str(descriptor.get("name", "") or "").strip()
        if not name:
            continue
        gateway.set_route(name, "desktop")
        tools.append(_build_structured_tool(descriptor, gateway, ctx))
    return tools


def _build_structured_tool(
    descriptor: dict[str, Any],
    gateway: ToolGateway,
    ctx: ToolContext,
) -> StructuredTool:
    name = str(descriptor.get("name", "") or "").strip()
    description = _tool_description(descriptor)
    args_schema = _build_args_schema(name, descriptor.get("parameters"))

    async def _invoke(**kwargs: Any) -> str:
        result = await gateway.execute(
            ToolCall(
                tool_name=name,
                params=kwargs,
                task_id=ctx.task_id,
            ),
            ctx,
        )
        return _serialize_tool_result(result)

    return StructuredTool.from_function(
        coroutine=_invoke,
        name=name,
        description=description,
        args_schema=args_schema,
    )


def _build_args_schema(tool_name: str, schema: Any) -> type[BaseModel]:
    parameters = schema if isinstance(schema, dict) else {}
    properties = parameters.get("properties") if isinstance(parameters.get("properties"), dict) else {}
    required = set(parameters.get("required") or []) if isinstance(parameters.get("required"), list) else set()

    fields: dict[str, tuple[Any, Any]] = {}
    for raw_name, prop_schema in properties.items():
        field_name = str(raw_name)
        prop = prop_schema if isinstance(prop_schema, dict) else {}
        annotation = _annotation_from_schema(prop)
        if field_name not in required:
            annotation = annotation | None
            default: Any = None
        else:
            default = ...
        description = str(prop.get("description", "") or "")
        if description:
            default = Field(default, description=description)
        fields[field_name] = (annotation, default)

    model_name = f"{_sanitize_model_name(tool_name)}Args"
    return create_model(model_name, **fields)  # type: ignore[arg-type]


def _annotation_from_schema(schema: dict[str, Any]) -> Any:
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        literal_values = tuple(value for value in enum_values if isinstance(value, (str, int, float, bool)))
        if literal_values:
            return Literal.__getitem__(literal_values)

    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        raw_type = non_null[0] if non_null else "object"

    if raw_type == "string":
        return str
    if raw_type == "integer":
        return int
    if raw_type == "number":
        return float
    if raw_type == "boolean":
        return bool
    if raw_type == "array":
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        item_annotation = _annotation_from_schema(item_schema)
        return list[item_annotation]
    if raw_type == "object":
        return dict[str, Any]
    return Any


def _serialize_tool_result(result: ToolResult) -> str:
    payload: dict[str, Any] = {
        "success": bool(result.success),
        "output": _compact_output(result),
        "artifacts": [_summarize_artifact(artifact) for artifact in result.artifacts[:5]],
    }
    if result.error:
        payload["error"] = result.error
    if result.execution_time_ms:
        payload["execution_time_ms"] = result.execution_time_ms
    return json.dumps(payload, ensure_ascii=False)


def _compact_output(result: ToolResult) -> str:
    output = str(result.output or "")
    if any(isinstance(artifact, dict) and artifact.get("data") for artifact in result.artifacts):
        return output[:200] + ("…（inline artifact omitted）" if len(output) > 200 else "")
    return output[:4000] + ("…" if len(output) > 4000 else "")


def _summarize_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "name": str(artifact.get("name", "") or "artifact"),
    }
    if artifact.get("type") is not None:
        summary["type"] = artifact.get("type")
    if artifact.get("path"):
        summary["path"] = artifact.get("path")
    mime = artifact.get("mime") or artifact.get("mime_type")
    if mime:
        summary["mime"] = mime
    if artifact.get("data"):
        summary["inline_data"] = True
        summary["data_length"] = len(str(artifact.get("data") or ""))
    return summary


def _sanitize_model_name(tool_name: str) -> str:
    compact = re.sub(r"[^a-zA-Z0-9]+", "_", tool_name).strip("_")
    return compact.title() or "DesktopTool"


def _tool_description(descriptor: dict[str, Any]) -> str:
    name = str(descriptor.get("name", "") or "").strip()
    base = str(descriptor.get("description", "") or f"Desktop tool: {name}").strip()
    permission = str(descriptor.get("permission_level", "") or "")

    if name in {"list_dir", "file_read", "file_search", "grep"}:
        return (
            f"{base} Preferred for read-only local file inspection tasks. "
            "Use this instead of shell whenever it can satisfy the request."
        ).strip()

    if name == "shell":
        return (
            f"{base} Dangerous. Only use when dedicated tools like list_dir/file_read/file_search/grep "
            "cannot solve the task. Never use shell for simple directory listing or file inspection."
        ).strip()

    if permission == "dangerous":
        return (
            f"{base} Dangerous. Use only if no safer tool can accomplish the task and the operation is truly necessary."
        ).strip()

    return base
