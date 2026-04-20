"""MiniMax OpenAI-compatible adapter."""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.utils.function_calling import convert_to_openai_tool
from openai import AsyncOpenAI, OpenAI
from pydantic import PrivateAttr

from agent.brain.providers.base import ProviderChatModelAdapter
from agent.brain.providers._utils import debug_body_preview

log = logging.getLogger("chatdada.llm")


def _normalize_minimax_temperature(value: Any) -> float | Any:
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        return value
    if 0.0 < temperature <= 1.0:
        return temperature
    return 1.0


def _apply_minimax_defaults(kwargs: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(kwargs)

    extra_body = dict(normalized.get("extra_body") or {})
    extra_body.setdefault("reasoning_split", True)
    normalized["extra_body"] = extra_body

    if normalized.get("temperature") is not None:
        normalized["temperature"] = _normalize_minimax_temperature(normalized["temperature"])

    normalized.setdefault("disable_streaming", "tool_calling")
    return normalized


def _find_wrapped_chat_openai(obj: Any) -> Any | None:
    stack = [obj]
    seen: set[int] = set()

    while stack:
        current = stack.pop()
        if current is None:
            continue
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)

        if hasattr(current, "async_client") and hasattr(current, "client") and hasattr(current, "_get_request_payload"):
            return current

        for attr in ("_llm", "bound", "first", "last"):
            child = getattr(current, attr, None)
            if child is not None:
                stack.append(child)

        middle = getattr(current, "middle", None)
        if isinstance(middle, list):
            stack.extend(middle)

    return None


def _message_reasoning_details(message: Any) -> list[dict[str, Any]]:
    additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
    details = additional_kwargs.get("reasoning_details", getattr(message, "reasoning_details", None))
    if isinstance(details, list):
        return [item for item in details if isinstance(item, dict)]
    return []


def _merge_reasoning_details_into_payload(payload: dict[str, Any], original_messages: list[Any] | None) -> dict[str, Any]:
    if not original_messages:
        return _collapse_minimax_system_messages(payload)

    messages_payload = payload.get("messages")
    if not isinstance(messages_payload, list):
        return _collapse_minimax_system_messages(payload)

    merged = dict(payload)
    merged_messages: list[Any] = []
    for original, serialized in zip(original_messages, messages_payload):
        if not isinstance(serialized, dict):
            merged_messages.append(serialized)
            continue
        details = _message_reasoning_details(original)
        if details and str(serialized.get("role", "")) == "assistant":
            merged_messages.append({**serialized, "reasoning_details": details})
        else:
            merged_messages.append(serialized)
    if len(messages_payload) > len(merged_messages):
        merged_messages.extend(messages_payload[len(merged_messages) :])
    merged["messages"] = merged_messages
    return _collapse_minimax_system_messages(merged)


def _collapse_minimax_system_messages(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return payload

    prefix_parts: list[str] = []
    collapsed_messages: list[Any] = []
    consuming_prefix = True

    for message in messages:
        if not isinstance(message, dict):
            consuming_prefix = False
            collapsed_messages.append(message)
            continue

        role = str(message.get("role", "") or "")
        content = message.get("content", "")
        if consuming_prefix and role in {"system", "developer"}:
            if isinstance(content, str) and content.strip():
                prefix_parts.append(content.strip())
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str) and block.get("text", "").strip():
                        prefix_parts.append(str(block["text"]).strip())
            continue

        consuming_prefix = False
        collapsed_messages.append(message)

    if len(prefix_parts) <= 1:
        return payload

    merged = dict(payload)
    merged["messages"] = [{"role": "system", "content": "\n\n".join(prefix_parts)}] + collapsed_messages
    return merged


def _log_minimax_payload_summary(model: str, payload: dict[str, Any]) -> None:
    messages = payload.get("messages")
    roles: list[str] = []
    system_count = 0
    if isinstance(messages, list):
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "") or "")
            roles.append(role)
            if role == "system":
                system_count += 1
    tools = payload.get("tools")
    tool_count = len(tools) if isinstance(tools, list) else 0
    extra_body = payload.get("extra_body") or {}
    log.info(
        "MiniMax request summary for %s: roles=%s system_count=%s tool_count=%s reasoning_split=%s",
        model,
        roles,
        system_count,
        tool_count,
        bool(isinstance(extra_body, dict) and extra_body.get("reasoning_split")),
    )


def _minimax_usage_metadata(raw_usage: dict[str, Any]) -> dict[str, Any]:
    prompt_tokens = int(raw_usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(raw_usage.get("completion_tokens", 0) or 0)
    total_tokens = int(raw_usage.get("total_tokens", 0) or (prompt_tokens + completion_tokens))

    output_details: dict[str, int] = {}
    completion_details = raw_usage.get("completion_tokens_details") or {}
    if isinstance(completion_details, dict):
        reasoning_tokens = completion_details.get("reasoning_tokens")
        if reasoning_tokens is not None:
            output_details["reasoning"] = int(reasoning_tokens or 0)

    metadata: dict[str, Any] = {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    if output_details:
        metadata["output_token_details"] = output_details
    return metadata


def _reasoning_text_from_details(details: list[dict[str, Any]]) -> str:
    parts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            if value:
                parts.append(value)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"type", "id", "format", "index"}:
                    continue
                if key == "text" and isinstance(item, str):
                    text = str(item or "")
                    if text:
                        parts.append(text)
                    continue
                visit(item)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)

    visit(details)
    return "\n".join(part for part in parts if part).strip()


def _message_content_to_minimax_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _langchain_message_to_minimax_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": str(message.tool_call_id),
            "content": _message_content_to_minimax_text(message.content),
        }

    role = "user"
    if isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, HumanMessage):
        role = "user"
    elif isinstance(message, AIMessage):
        role = "assistant"
    else:
        role = str(getattr(message, "role", "user") or "user")

    payload: dict[str, Any] = {
        "role": role,
        "content": _message_content_to_minimax_text(getattr(message, "content", "")),
    }

    if role == "assistant":
        tool_calls = list(getattr(message, "tool_calls", []) or [])
        if tool_calls:
            payload["tool_calls"] = [
                {
                    "id": str(call.get("id", "") or ""),
                    "type": "function",
                    "function": {
                        "name": str(call.get("name", "") or ""),
                        "arguments": json.dumps(call.get("args", {}) or {}, ensure_ascii=False),
                    },
                }
                for call in tool_calls
            ]
            payload["content"] = payload["content"] or None

        reasoning_details = _message_reasoning_details(message)
        if reasoning_details:
            payload["reasoning_details"] = reasoning_details

    return payload


def _minimax_response_to_ai_message(parsed_payload: dict[str, Any]) -> AIMessage:
    choices = parsed_payload.get("choices") or []
    if not choices or not isinstance(choices, list):
        raise ValueError(f"MiniMax response missing choices: {parsed_payload}")

    choice0 = choices[0] if isinstance(choices[0], dict) else {}
    raw_message = choice0.get("message") or {}
    raw_usage = parsed_payload.get("usage") or {}
    reasoning_details = raw_message.get("reasoning_details") or []
    raw_tool_calls = raw_message.get("tool_calls") or []

    tool_calls: list[dict[str, Any]] = []
    invalid_tool_calls: list[dict[str, Any]] = []
    for raw_call in raw_tool_calls:
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function") or {}
        raw_args = function.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except Exception as exc:
            invalid_tool_calls.append(
                {
                    "type": "invalid_tool_call",
                    "name": str(function.get("name", "") or ""),
                    "args": raw_args,
                    "id": str(raw_call.get("id", "") or ""),
                    "error": str(exc),
                }
            )
            continue
        tool_calls.append(
            {
                "name": str(function.get("name", "") or ""),
                "args": args if isinstance(args, dict) else {"__arg1": args},
                "id": str(raw_call.get("id", "") or ""),
                "type": "tool_call",
            }
        )

    response_metadata: dict[str, Any] = {
        "finish_reason": choice0.get("finish_reason"),
        "model_name": parsed_payload.get("model"),
        "model_provider": "openai",
        "_minimax_parsed_payload": parsed_payload,
    }
    if parsed_payload.get("id") is not None:
        response_metadata["id"] = parsed_payload.get("id")
    if isinstance(raw_usage, dict) and raw_usage:
        response_metadata["usage"] = raw_usage
        response_metadata["token_usage"] = raw_usage

    additional_kwargs: dict[str, Any] = {"_minimax_parsed_payload": parsed_payload}
    if isinstance(reasoning_details, list) and reasoning_details:
        additional_kwargs["reasoning_details"] = reasoning_details
        reasoning_text = _reasoning_text_from_details(reasoning_details)
        if reasoning_text:
            additional_kwargs["reasoning_content"] = reasoning_text
        response_metadata["reasoning_details"] = reasoning_details

    usage_metadata = _minimax_usage_metadata(raw_usage) if isinstance(raw_usage, dict) and raw_usage else None
    return AIMessage(
        content=_message_content_to_minimax_text(raw_message.get("content", "")),
        additional_kwargs=additional_kwargs,
        response_metadata=response_metadata,
        usage_metadata=usage_metadata,
        tool_calls=tool_calls,
        invalid_tool_calls=invalid_tool_calls,
        id=str(parsed_payload.get("id", "") or "") or None,
    )


def _parse_structured_output(schema: Any, text: str) -> Any:
    if hasattr(schema, "model_validate_json"):
        return schema.model_validate_json(text)
    return json.loads(text)


class _MiniMaxStructuredOutputAdapter:
    def __init__(self, adapter: "MiniMaxOpenAIAdapter", schema: Any) -> None:
        self._adapter = adapter
        self._schema = schema

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._adapter.ainvoke(*args, **kwargs)
        return _parse_structured_output(self._schema, _message_content_to_minimax_text(response.content))

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        response = self._adapter.invoke(*args, **kwargs)
        return _parse_structured_output(self._schema, _message_content_to_minimax_text(response.content))


class MiniMaxOpenAIAdapter(ProviderChatModelAdapter):
    """Minimal MiniMax chat adapter backed by direct OpenAI SDK calls."""

    use_responses_api: ClassVar[bool] = False
    _base_url: str | None = PrivateAttr(default=None)
    _sync_client: Any = PrivateAttr(default=None)
    _async_client: Any = PrivateAttr(default=None)
    _request_defaults: dict[str, Any] = PrivateAttr(default_factory=dict)
    _bound_tools: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _tool_choice: Any = PrivateAttr(default=None)

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        llm: "MiniMaxOpenAIAdapter | None" = None,
        bound_tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        request_defaults: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model=model)
        self._base_url = base_url

        if llm is not None:
            self._sync_client = llm._sync_client
            self._async_client = llm._async_client
            self._request_defaults = dict(request_defaults or llm._request_defaults)
            self._bound_tools = list(bound_tools if bound_tools is not None else llm._bound_tools)
            self._tool_choice = llm._tool_choice if tool_choice is None else tool_choice
            return

        if not api_key:
            raise ValueError("MiniMaxOpenAIAdapter requires an api_key when llm is not provided")

        timeout = kwargs.pop("timeout", None)
        max_retries = kwargs.pop("max_retries", None)
        self._sync_client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._async_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._request_defaults = dict(request_defaults or kwargs)
        self._bound_tools = list(bound_tools or [])
        self._tool_choice = tool_choice

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return self.model

    def _build_tool_choice(self, raw_tool_choice: Any) -> Any:
        if raw_tool_choice in (None, False):
            return None
        if raw_tool_choice is True:
            return "required"
        if isinstance(raw_tool_choice, str):
            if raw_tool_choice == "any":
                return "required"
            tool_names = [tool.get("function", {}).get("name") for tool in self._bound_tools]
            if raw_tool_choice in tool_names:
                return {"type": "function", "function": {"name": raw_tool_choice}}
            return raw_tool_choice
        return raw_tool_choice

    def _prepare_request_kwargs(self, runtime_kwargs: dict[str, Any]) -> dict[str, Any]:
        merged = dict(self._request_defaults)
        merged.update(runtime_kwargs)
        merged = _apply_minimax_defaults(merged)

        for key in (
            "disable_streaming",
            "use_responses_api",
            "output_version",
            "thinking_level",
            "reasoning_effort",
            "stream_usage",
            "parallel_tool_calls",
            "response_format",
            "strict",
        ):
            merged.pop(key, None)

        if "max_tokens" in merged and "max_completion_tokens" not in merged:
            merged["max_completion_tokens"] = merged.pop("max_tokens")
        return merged

    def _build_payload(self, messages: list[Any], runtime_kwargs: dict[str, Any]) -> dict[str, Any]:
        request_kwargs = self._prepare_request_kwargs(runtime_kwargs)
        stop = request_kwargs.pop("stop", None)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_langchain_message_to_minimax_dict(message) for message in messages],
            **request_kwargs,
        }
        if stop is not None:
            payload["stop"] = stop
        if self._bound_tools:
            payload["tools"] = list(self._bound_tools)
            resolved_tool_choice = self._build_tool_choice(self._tool_choice)
            if resolved_tool_choice is not None:
                payload["tool_choice"] = resolved_tool_choice
        payload = _merge_reasoning_details_into_payload(payload, messages)
        _log_minimax_payload_summary(self.model, payload)
        return payload

    def _invoke_message(
        self,
        messages: list[Any],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        payload = self._build_payload(messages, {"stop": stop, **kwargs} if stop is not None else kwargs)
        response = self._sync_client.chat.completions.create(**payload)
        parsed_payload = response.model_dump(exclude_none=True, mode="json")
        result = _minimax_response_to_ai_message(parsed_payload)
        if getattr(result, "usage_metadata", None) is None:
            log.warning(
                "MiniMax response missing usage for %s: keys=%s response=%s",
                self.model,
                sorted(parsed_payload.keys()),
                debug_body_preview(parsed_payload),
            )
        return result

    async def _ainvoke_message(
        self,
        messages: list[Any],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        payload = self._build_payload(messages, {"stop": stop, **kwargs} if stop is not None else kwargs)
        response = await self._async_client.chat.completions.create(**payload)
        parsed_payload = response.model_dump(exclude_none=True, mode="json")
        result = _minimax_response_to_ai_message(parsed_payload)
        if getattr(result, "usage_metadata", None) is None:
            log.warning(
                "MiniMax response missing usage for %s: keys=%s response=%s",
                self.model,
                sorted(parsed_payload.keys()),
                debug_body_preview(parsed_payload),
            )
        return result

    async def astream(self, *args: Any, **kwargs: Any):
        result = await self.ainvoke(*args, **kwargs)
        chunk = AIMessageChunk(
            content=_message_content_to_minimax_text(result.content),
            additional_kwargs=dict(result.additional_kwargs or {}),
            response_metadata=dict(result.response_metadata or {}),
            usage_metadata=result.usage_metadata,
            id=result.id,
        )
        yield chunk

    def bind_tools(
        self,
        tools: list[Any],
        *,
        tool_choice: Any | None = None,
        strict: bool | None = None,
        **kwargs: Any,
    ) -> "MiniMaxOpenAIAdapter":
        formatted_tools = [convert_to_openai_tool(tool, strict=strict) for tool in tools]
        request_defaults = dict(self._request_defaults)
        request_defaults.update(kwargs)
        return MiniMaxOpenAIAdapter(
            self.model,
            base_url=self._base_url,
            llm=self,
            bound_tools=formatted_tools,
            tool_choice=tool_choice,
            request_defaults=request_defaults,
        )

    def with_structured_output(self, schema: Any, *args: Any, **kwargs: Any) -> _MiniMaxStructuredOutputAdapter:
        return _MiniMaxStructuredOutputAdapter(self, schema)


__all__ = [
    "MiniMaxOpenAIAdapter",
    "_apply_minimax_defaults",
]
