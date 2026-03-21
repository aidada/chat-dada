from __future__ import annotations

from typing import Any


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


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
    part_type = str(part.get("type", ""))
    data = part.get("data") or {}
    graph_node = ".".join(str(item) for item in (part.get("ns") or ())) or "root"
    base = _base_payload(
        thread_id=thread_id,
        domain=domain,
        graph_node=graph_node,
        checkpoint_id=checkpoint_id,
        trace_metadata=trace_metadata,
    )

    if part_type == "custom":
        event_type = str(data.get("event_type") or data.get("type") or "step")
        payload = dict(data)
        payload.pop("event_type", None)
        payload["type"] = event_type
        payload.update(base)
        translated.append((event_type, payload))
        return translated

    if part_type == "updates":
        interrupts = data.get("__interrupt__")
        if interrupts:
            first = interrupts[0]
            value = getattr(first, "value", first)
            payload = dict(value if isinstance(value, dict) else {"content": str(value)})
            payload.setdefault("content", str(payload.get("content", "")))
            payload["interrupt_type"] = "human_input"
            payload.update(base)
            translated.append(("question", payload))
        return translated

    if part_type == "messages":
        # Token-by-token LLM output: extract content from AIMessageChunk
        message = data
        if hasattr(message, "content"):
            content = message.content
        elif isinstance(data, dict):
            content = data.get("content", "")
        else:
            content = ""
        if isinstance(content, list):
            text_parts = [
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content
            ]
            content = "".join(text_parts)
        content = str(content)
        if content:
            payload = {"content": content}
            payload.update(base)
            translated.append(("token", payload))
        return translated

    if part_type == "tasks":
        if data.get("error"):
            payload = {"content": str(data["error"])}
            payload.update(base)
            translated.append(("error", payload))
        return translated

    return translated


def extract_checkpoint_id(part: dict[str, Any]) -> str:
    if part.get("type") != "checkpoints":
        return ""
    data = part.get("data") or {}
    config = data.get("config") or {}
    configurable = config.get("configurable") or {}
    return str(configurable.get("checkpoint_id", "") or "")
