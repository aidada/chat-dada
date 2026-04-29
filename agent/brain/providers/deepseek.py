"""DeepSeek OpenAI-compatible adapter."""

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

from agent.brain.providers._utils import debug_body_preview
from agent.brain.providers.base import ProviderChatModelAdapter

log = logging.getLogger("chatdada.llm")


def _normalize_deepseek_reasoning_effort(thinking_level: Any) -> str:
    level = str(thinking_level or "").strip().lower()
    if level in {"xhigh", "max"}:
        return "max"
    return "high"


def _apply_deepseek_defaults(kwargs: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(kwargs)
    thinking_level = normalized.pop("thinking_level", None)

    extra_body = dict(normalized.get("extra_body") or {})
    raw_thinking = extra_body.get("thinking")
    thinking = dict(raw_thinking) if isinstance(raw_thinking, dict) else {}
    thinking_type = str(thinking.get("type") or "enabled").strip().lower()
    if thinking_type not in {"enabled", "disabled"}:
        thinking_type = "enabled"
    thinking["type"] = thinking_type
    extra_body["thinking"] = thinking
    normalized["extra_body"] = extra_body

    if thinking_type == "enabled":
        normalized.setdefault("reasoning_effort", _normalize_deepseek_reasoning_effort(thinking_level))
        for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
            normalized.pop(key, None)
    else:
        normalized.pop("reasoning_effort", None)

    return normalized


def _message_content_to_deepseek_text(content: Any) -> str:
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


def _message_reasoning_content(message: Any) -> str | None:
    additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
    reasoning_content = additional_kwargs.get("reasoning_content", getattr(message, "reasoning_content", None))
    if isinstance(reasoning_content, str) and reasoning_content:
        return reasoning_content
    return None


def _langchain_message_to_deepseek_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": str(message.tool_call_id),
            "content": _message_content_to_deepseek_text(message.content),
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
        "content": _message_content_to_deepseek_text(getattr(message, "content", "")),
    }

    if role == "assistant":
        reasoning_content = _message_reasoning_content(message)
        if reasoning_content:
            payload["reasoning_content"] = reasoning_content

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

    return payload


def _deepseek_usage_metadata(raw_usage: dict[str, Any]) -> dict[str, Any]:
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


def _deepseek_response_to_ai_message(parsed_payload: dict[str, Any]) -> AIMessage:
    choices = parsed_payload.get("choices") or []
    if not choices or not isinstance(choices, list):
        raise ValueError(f"DeepSeek response missing choices: {parsed_payload}")

    choice0 = choices[0] if isinstance(choices[0], dict) else {}
    raw_message = choice0.get("message") or {}
    raw_usage = parsed_payload.get("usage") or {}
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
        "_deepseek_parsed_payload": parsed_payload,
    }
    if parsed_payload.get("id") is not None:
        response_metadata["id"] = parsed_payload.get("id")
    if isinstance(raw_usage, dict) and raw_usage:
        response_metadata["usage"] = raw_usage
        response_metadata["token_usage"] = raw_usage

    additional_kwargs: dict[str, Any] = {"_deepseek_parsed_payload": parsed_payload}
    reasoning_content = raw_message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        additional_kwargs["reasoning_content"] = reasoning_content
        response_metadata["reasoning_content"] = reasoning_content

    usage_metadata = _deepseek_usage_metadata(raw_usage) if isinstance(raw_usage, dict) and raw_usage else None
    return AIMessage(
        content=_message_content_to_deepseek_text(raw_message.get("content", "")),
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


class _DeepSeekStructuredOutputAdapter:
    def __init__(self, adapter: "DeepSeekOpenAIAdapter", schema: Any) -> None:
        self._adapter = adapter
        self._schema = schema

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("response_format", {"type": "json_object"})
        response = await self._adapter.ainvoke(*args, **kwargs)
        return _parse_structured_output(self._schema, _message_content_to_deepseek_text(response.content))

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("response_format", {"type": "json_object"})
        response = self._adapter.invoke(*args, **kwargs)
        return _parse_structured_output(self._schema, _message_content_to_deepseek_text(response.content))


class DeepSeekOpenAIAdapter(ProviderChatModelAdapter):
    """DeepSeek chat adapter backed by direct OpenAI SDK calls."""

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
        llm: "DeepSeekOpenAIAdapter | None" = None,
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
            raise ValueError("DeepSeekOpenAIAdapter requires an api_key when llm is not provided")

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
        merged = _apply_deepseek_defaults(merged)

        for key in (
            "disable_streaming",
            "use_responses_api",
            "output_version",
            "stream_usage",
            "parallel_tool_calls",
            "strict",
        ):
            merged.pop(key, None)
        return merged

    def _build_payload(self, messages: list[Any], runtime_kwargs: dict[str, Any]) -> dict[str, Any]:
        request_kwargs = self._prepare_request_kwargs(runtime_kwargs)
        stop = request_kwargs.pop("stop", None)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_langchain_message_to_deepseek_dict(message) for message in messages],
            **request_kwargs,
        }
        if stop is not None:
            payload["stop"] = stop
        if self._bound_tools:
            payload["tools"] = list(self._bound_tools)
            resolved_tool_choice = self._build_tool_choice(self._tool_choice)
            if resolved_tool_choice is not None:
                payload["tool_choice"] = resolved_tool_choice
        self._log_payload_summary(payload)
        return payload

    def _log_payload_summary(self, payload: dict[str, Any]) -> None:
        messages = payload.get("messages")
        roles: list[str] = []
        if isinstance(messages, list):
            for item in messages:
                if isinstance(item, dict):
                    roles.append(str(item.get("role", "") or ""))
        tools = payload.get("tools")
        tool_count = len(tools) if isinstance(tools, list) else 0
        extra_body = payload.get("extra_body") or {}
        thinking_enabled = (
            isinstance(extra_body, dict)
            and isinstance(extra_body.get("thinking"), dict)
            and extra_body["thinking"].get("type") == "enabled"
        )
        log.info(
            "DeepSeek request summary for %s: roles=%s tool_count=%s thinking=%s response_format=%s",
            self.model,
            roles,
            tool_count,
            thinking_enabled,
            payload.get("response_format"),
        )

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
        result = _deepseek_response_to_ai_message(parsed_payload)
        if getattr(result, "usage_metadata", None) is None:
            log.warning(
                "DeepSeek response missing usage for %s: keys=%s response=%s",
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
        result = _deepseek_response_to_ai_message(parsed_payload)
        if getattr(result, "usage_metadata", None) is None:
            log.warning(
                "DeepSeek response missing usage for %s: keys=%s response=%s",
                self.model,
                sorted(parsed_payload.keys()),
                debug_body_preview(parsed_payload),
            )
        return result

    async def astream(self, *args: Any, **kwargs: Any):
        result = await self.ainvoke(*args, **kwargs)
        chunk = AIMessageChunk(
            content=_message_content_to_deepseek_text(result.content),
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
    ) -> "DeepSeekOpenAIAdapter":
        formatted_tools = [convert_to_openai_tool(tool, strict=strict) for tool in tools]
        request_defaults = dict(self._request_defaults)
        request_defaults.update(kwargs)
        return DeepSeekOpenAIAdapter(
            self.model,
            base_url=self._base_url,
            llm=self,
            bound_tools=formatted_tools,
            tool_choice=tool_choice,
            request_defaults=request_defaults,
        )

    def with_structured_output(self, schema: Any, *args: Any, **kwargs: Any) -> _DeepSeekStructuredOutputAdapter:
        return _DeepSeekStructuredOutputAdapter(self, schema)


__all__ = [
    "DeepSeekOpenAIAdapter",
    "_apply_deepseek_defaults",
]
