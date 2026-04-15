from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from langgraph.types import Command


STREAM_SCHEMA_VERSION = "2026-03-23"
_UPDATE_INTERNAL_KEYS = {"__interrupt__", "__metadata__"}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _namespace(part: dict[str, Any]) -> tuple[str, ...]:
    raw = part.get("ns") or ()
    return tuple(str(item) for item in raw)


def _graph_node_from_namespace(ns: Sequence[str]) -> str:
    return ".".join(str(item) for item in ns) or "root"


def _base_part_payload(
    part: dict[str, Any],
    *,
    checkpoint_id: str = "",
    graph_node: str | None = None,
) -> dict[str, Any]:
    ns = _namespace(part)
    payload = {
        "stream_schema_version": STREAM_SCHEMA_VERSION,
        "stream_part_type": str(part.get("type", "")),
        "graph_path": list(ns),
        "graph_node": graph_node or _graph_node_from_namespace(ns),
    }
    if checkpoint_id:
        payload["checkpoint_id"] = checkpoint_id
    return payload


def _coerce_message_data(data: Any) -> tuple[Any, dict[str, Any]]:
    if isinstance(data, tuple) and len(data) == 2:
        message, metadata = data
        return message, metadata if isinstance(metadata, dict) else {"value": _jsonable(metadata)}
    if isinstance(data, list) and len(data) == 2:
        message, metadata = data
        return message, metadata if isinstance(metadata, dict) else {"value": _jsonable(metadata)}
    if isinstance(data, dict):
        return data, {}
    return data, {}


def _extract_message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content", "")
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        content = "".join(parts)
    return str(content)


def _normalize_custom_part(part: dict[str, Any], *, checkpoint_id: str = "") -> list[dict[str, Any]]:
    from agent.session.protocol import OLD_TO_NEW_TYPE_MAP

    data = part.get("data")
    if isinstance(data, dict):
        payload = {str(key): _jsonable(value) for key, value in data.items()}
        raw_type = str(payload.pop("event_type", payload.get("type") or "custom"))
        payload.pop("type", None)
    else:
        payload = {"content": str(data)}
        raw_type = "custom"

    # 通过映射表将旧类型名自动转换为新的 category.action 格式
    event_type = OLD_TO_NEW_TYPE_MAP.get(raw_type, raw_type)

    if event_type == "artifact.created":
        payload.setdefault("content", str(payload.get("name") or payload.get("url") or ""))
    elif "content" not in payload and event_type not in {"progress.step", "progress.node", "progress.checkpoint"}:
        payload["content"] = str(data)

    base = _base_part_payload(
        part,
        checkpoint_id=str(payload.get("checkpoint_id", checkpoint_id) or checkpoint_id),
        graph_node=str(payload.get("graph_node", "") or "") or None,
    )
    merged = dict(payload)
    for key, value in base.items():
        merged.setdefault(key, value)
    merged["event_type"] = event_type
    return [merged]


def _normalize_update_part(part: dict[str, Any], *, checkpoint_id: str = "") -> list[dict[str, Any]]:
    data = part.get("data") or {}
    events: list[dict[str, Any]] = []
    base = _base_part_payload(part, checkpoint_id=checkpoint_id)

    interrupts = data.get("__interrupt__")
    if interrupts:
        first = interrupts[0]
        value = getattr(first, "value", first)
        payload = dict(value if isinstance(value, dict) else {"content": str(value)})
        payload.setdefault("content", str(payload.get("content", "")))
        payload["interrupt_type"] = str(payload.get("interrupt_type", "human_input"))
        payload["event_type"] = "interaction.question"
        payload.update(base)
        events.append(payload)

    update_metadata = _jsonable(data.get("__metadata__", {})) if "__metadata__" in data else {}
    for node_name, update in data.items():
        if node_name in _UPDATE_INTERNAL_KEYS:
            continue
        payload = {
            "event_type": "progress.node",
            "node_name": str(node_name),
            "status": "updated",
            "content": f"Node updated: {node_name}",
            "update": _jsonable(update),
        }
        if update_metadata:
            payload["update_metadata"] = update_metadata
        payload.update(base)
        events.append(payload)

    return events


def _extract_interrupt_payload(part: dict[str, Any]) -> dict[str, Any] | None:
    data = part.get("data") or {}
    interrupts = data.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    payload = dict(value if isinstance(value, dict) else {"content": str(value)})
    payload.setdefault("content", str(payload.get("content", "")))
    payload["interrupt_type"] = str(payload.get("interrupt_type", "human_input"))
    return payload


def _sync_parent_interrupt_state(consumed_count: int) -> None:
    if consumed_count <= 0:
        return
    try:
        from langgraph.types import interrupt
        for _ in range(consumed_count):
            interrupt({"_sync_only": True})
    except Exception:
        return


def _merge_update_values(final_values: Any, part: dict[str, Any]) -> Any:
    data = part.get("data") or {}
    merged = dict(final_values) if isinstance(final_values, dict) else {}
    for node_name, update in data.items():
        if node_name in _UPDATE_INTERNAL_KEYS:
            continue
        if isinstance(update, dict):
            merged.update(_jsonable(update))
        else:
            merged[str(node_name)] = _jsonable(update)
    return merged or final_values


def _normalize_message_part(part: dict[str, Any], *, checkpoint_id: str = "") -> list[dict[str, Any]]:
    message, metadata = _coerce_message_data(part.get("data"))
    content = _extract_message_text(message)
    if not content:
        return []

    raw_metadata = _jsonable(metadata)
    graph_node = ""
    if isinstance(raw_metadata, dict):
        graph_node = str(raw_metadata.get("langgraph_node", "") or "")

    payload = {
        "event_type": "content.delta",
        "text": content,          # spec 要求 payload.text（取代旧的 content 字段）
        "content": content,       # 保留 content 供内部兼容
        "message_metadata": raw_metadata,
    }
    payload.update(_base_part_payload(part, checkpoint_id=checkpoint_id, graph_node=graph_node or None))
    return [payload]


def _normalize_task_part(part: dict[str, Any], *, checkpoint_id: str = "") -> list[dict[str, Any]]:
    data = dict(part.get("data") or {})
    task_name = str(data.get("name", "") or "")
    is_start = "input" in data and "triggers" in data

    if is_start:
        status = "started"
        phase = "start"
        content = f"Task started: {task_name or data.get('id', 'unknown')}"
    else:
        error = str(data.get("error", "") or "")
        interrupts = _jsonable(data.get("interrupts", []) or [])
        if error:
            status = "failed"
            content = f"Task failed: {task_name or data.get('id', 'unknown')}"
        elif interrupts:
            status = "interrupted"
            content = f"Task interrupted: {task_name or data.get('id', 'unknown')}"
        else:
            status = "completed"
            content = f"Task completed: {task_name or data.get('id', 'unknown')}"
        phase = "finish"

    payload = {
        "event_type": "progress.step",  # langgraph task 进度 → 折叠为 progress.step
        "phase": phase,
        "status": status,
        "content": content,
        "langgraph_task_id": str(data.get("id", "") or ""),
        "task_name": task_name,
    }

    if is_start:
        payload["input"] = _jsonable(data.get("input"))
        payload["triggers"] = _jsonable(data.get("triggers", []) or [])
    else:
        payload["result"] = _jsonable(data.get("result", {}) or {})
        payload["error"] = str(data.get("error", "") or "")
        payload["interrupts"] = _jsonable(data.get("interrupts", []) or [])

    payload.update(_base_part_payload(part, checkpoint_id=checkpoint_id))
    return [payload]


def _normalize_checkpoint_part(part: dict[str, Any], *, checkpoint_id: str = "") -> list[dict[str, Any]]:
    data = dict(part.get("data") or {})
    checkpoint_value = extract_checkpoint_id(part) or checkpoint_id
    payload = {
        "event_type": "progress.checkpoint",
        "status": "saved",
        "content": f"Checkpoint saved: {checkpoint_value or 'unknown'}",
        "checkpoint_id": checkpoint_value,
        "next_nodes": _jsonable(data.get("next", []) or []),
        "checkpoint_tasks": _jsonable(data.get("tasks", []) or []),
        "checkpoint_metadata": _jsonable(data.get("metadata", {}) or {}),
    }
    payload.update(_base_part_payload(part, checkpoint_id=checkpoint_value))
    return [payload]


def normalize_stream_part(part: dict[str, Any], *, checkpoint_id: str = "") -> list[dict[str, Any]]:
    part_type = str(part.get("type", ""))
    if part_type == "custom":
        return _normalize_custom_part(part, checkpoint_id=checkpoint_id)
    if part_type == "updates":
        return _normalize_update_part(part, checkpoint_id=checkpoint_id)
    if part_type == "messages":
        return _normalize_message_part(part, checkpoint_id=checkpoint_id)
    if part_type == "tasks":
        return _normalize_task_part(part, checkpoint_id=checkpoint_id)
    if part_type == "checkpoints":
        return _normalize_checkpoint_part(part, checkpoint_id=checkpoint_id)
    return []


def _base_payload(
    *,
    thread_id: str,
    domain: str,
    graph_node: str,
    checkpoint_id: str,
    trace_metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "domain": domain,
        "graph_node": graph_node,
        "checkpoint_id": checkpoint_id,
        "trace_metadata": trace_metadata,
    }


def translate_stream_part(
    part: dict[str, Any],
    *,
    thread_id: str,
    domain: str,
    checkpoint_id: str,
    trace_metadata: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    translated: list[tuple[str, dict[str, Any]]] = []
    for normalized in normalize_stream_part(part, checkpoint_id=checkpoint_id):
        event_type = str(normalized.pop("event_type", "custom"))
        graph_node = str(normalized.get("graph_node", "") or "") or "root"
        event_checkpoint = str(normalized.get("checkpoint_id", "") or checkpoint_id)
        payload = dict(normalized)
        payload.update(
            _base_payload(
                thread_id=thread_id,
                domain=domain,
                graph_node=graph_node,
                checkpoint_id=event_checkpoint,
                trace_metadata=trace_metadata,
            )
        )
        payload["type"] = event_type
        if event_type == "artifact.created":
            payload.setdefault("content", str(payload.get("name") or payload.get("url") or ""))
        translated.append((event_type, payload))
    return translated


def extract_checkpoint_id(part: dict[str, Any]) -> str:
    if part.get("type") != "checkpoints":
        return ""
    data = part.get("data") or {}
    config = data.get("config") or {}
    configurable = config.get("configurable") or {}
    return str(configurable.get("checkpoint_id", "") or "")


async def stream_nested_graph(
    graph: Any,
    input_data: Any,
    *,
    config: dict[str, Any] | None = None,
    extra_payload: dict[str, Any] | None = None,
    stream_mode: Iterable[str] = ("values", "updates", "messages", "custom", "tasks", "checkpoints"),
    subgraphs: bool = True,
) -> Any:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:
        writer = lambda _payload: None

    consumed_interrupts = 0
    nested_resume_value: Any = None
    inherited_configurable: dict[str, Any] = {}
    try:
        from langgraph.config import get_config

        configurable = get_config().get("configurable", {}) or {}
        inherited_configurable = dict(configurable)
        consumed_interrupts = int(configurable.get("nested_interrupt_count", 0) or 0)
        nested_resume_value = configurable.get("nested_resume_value")
    except Exception:
        consumed_interrupts = 0
        nested_resume_value = None
        inherited_configurable = {}
    _sync_parent_interrupt_state(consumed_interrupts)

    merged_config = dict(config or {})
    merged_configurable = {
        **inherited_configurable,
        **dict((config or {}).get("configurable", {}) or {}),
    }
    nested_recursion_limit = merged_configurable.get("nested_recursion_limit")
    if "recursion_limit" not in merged_config and nested_recursion_limit not in (None, ""):
        try:
            merged_config["recursion_limit"] = int(nested_recursion_limit)
        except (TypeError, ValueError):
            pass
    if merged_configurable:
        merged_config["configurable"] = merged_configurable

    final_values: Any = None
    pending_interrupt_payload: dict[str, Any] | None = None
    async for part in graph.astream(
        Command(resume=nested_resume_value) if nested_resume_value is not None else input_data,
        config=merged_config or None,
        version="v2",
        stream_mode=list(stream_mode),
        subgraphs=subgraphs,
    ):
        if part.get("type") == "values":
            final_values = part.get("data")
            continue
        if part.get("type") == "updates":
            interrupt_payload = _extract_interrupt_payload(part)
            if interrupt_payload is not None:
                pending_interrupt_payload = pending_interrupt_payload or interrupt_payload
            final_values = _merge_update_values(final_values, part)

        event_checkpoint = extract_checkpoint_id(part)
        for normalized in normalize_stream_part(part, checkpoint_id=event_checkpoint):
            payload = dict(normalized)
            payload.update(extra_payload or {})
            writer(payload)

    if pending_interrupt_payload is not None:
        from agent.platform.interrupts import request_interrupt

        request_interrupt(pending_interrupt_payload)

    if merged_config and hasattr(graph, "aget_state"):
        try:
            state_snapshot = await graph.aget_state(merged_config)
            snapshot_values = getattr(state_snapshot, "values", None)
            if isinstance(final_values, dict) and isinstance(snapshot_values, dict):
                final_values = {**final_values, **snapshot_values}
            elif snapshot_values not in (None, {}):
                final_values = snapshot_values
        except Exception:
            pass

    return final_values
