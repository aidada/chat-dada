"""
Model registry — centralized LLM configuration for all agents.
Each agent role gets its own model config. Change models per-role here.

Required environment variables (set in .env or shell):
    CO_API_KEY        — for "proxy" provider  (co.yes.vg, handles OpenAI + Gemini via proxy)
    OPENAI_API_KEY    — for "openai" provider (api.openai.com, native)
    MINIMAX_API_KEY   — for "minimax" provider (api.minimaxi.com, OpenAI-compatible)
    MOONSHOT_API_KEY  — for "moonshot" provider (api.moonshot.cn, Kimi native)
    GOOGLE_API_KEY    — for "google" provider  (Gemini native, no proxy)
    ANTHROPIC_API_KEY — for "anthropic" provider (Claude native)
Optional environment variables:
    MINIMAX_BASE_URL        — override MiniMax OpenAI-compatible endpoint
                              (default: https://api.minimaxi.com/v1)
    YESCODE_GEMINI_BASE_URL — override yescode Gemini endpoint
                              (default: https://co.yes.vg/gemini)

Adding a new provider:
    1. Add an entry to PROVIDERS with client/endpoint_url/api_key_env
    2. Add a branch in _build_client() if it's a new client type
    3. Reference the provider name in MODEL_CONFIGS
"""

import json
import logging
import os
from contextvars import ContextVar
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI, OpenAI

# Current request's thinking level, set by the WebSocket layer
_thinking_level: ContextVar[str] = ContextVar("thinking_level", default="medium")
DEFAULT_LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "7200"))
DEFAULT_LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "2"))
GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS = {"low", "high"}


log = logging.getLogger("chatdada.llm")

def set_thinking_level(level: str) -> None:
    """Set thinking level for current async context. Called from WebSocket handler."""
    _thinking_level.set(level)


# ── Provider definitions ─────────────────────────────────────────────────────
# client: which LangChain class to use. Currently supported:
#   "openai"    → ChatOpenAI  (also works for any OpenAI-compatible API)
#   "minimax_openai" → ChatOpenAI for MiniMax OpenAI-compatible API
#   "google"    → ChatGoogleGenerativeAI (langchain-google-genai, native Gemini)
#   "anthropic" → ChatAnthropic (langchain-anthropic, native Claude)
PROVIDERS: dict[str, dict] = {
    "proxy": {
        "client": "openai",
        "endpoint_url": "https://co.yes.vg/v1/responses",
        "api_key_env": "CO_API_KEY",
    },
    "openai": {
        "client": "openai",
        "api_key_env": "OPENAI_API_KEY",
    },
    "minimax": {
        "client": "minimax_openai",
        "endpoint_url": "https://api.minimaxi.com/v1",
        "endpoint_url_env": "MINIMAX_BASE_URL",
        "api_key_env": "MINIMAX_API_KEY",
    },
    "moonshot": {
        "client": "openai",  # Kimi is OpenAI-compatible
        "endpoint_url": "https://api.moonshot.cn/v1/chat/completions",
        "api_key_env": "MOONSHOT_API_KEY",
    },
    "google_proxy": {
        "client": "gemini_openai_adapter",  # yescode Gemini proxy with request/response logging
        "endpoint_url": "https://co.yes.vg/gemini",
        "endpoint_url_env": "YESCODE_GEMINI_BASE_URL",
        "api_key_env": "CO_API_KEY",
    },
    "anthropic": {
        "client": "anthropic",  # native Claude
        "api_key_env": "ANTHROPIC_API_KEY",
    },
}


# ── Role → model mapping ─────────────────────────────────────────────────────
# To swap a model: change "model" and/or "provider". Do not add credentials here.
MODEL_CONFIGS: dict[str, dict] = {
    "orchestrator": {"model": "gpt-5.4", "provider": "proxy"},
    "search": {"model": "gpt-5.4", "provider": "proxy"},
    "doc_analyst": {"model": "gemini-3.1-pro-preview-customtools", "provider": "google_proxy"},
    "writer": {"model": "gpt-5.4", "provider": "proxy"},
    "deep_research": {"model": "gpt-5.4", "provider": "proxy"},
    "data_analyst": {"model": "gpt-5.4", "provider": "proxy"},
    # 新增
    "research_domain": {"model": "MiniMax-M2.7", "provider": "minimax"},
    "patent_domain": {"model": "gpt-5.4", "provider": "proxy"},
    "zero_report_domain": {"model": "gpt-5.4", "provider": "proxy"},
}


def _debug_body_preview(body: Any, limit: int = 4000) -> str:
    if body is None:
        return "<empty>"
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    elif isinstance(body, (dict, list, tuple)):
        try:
            text = json.dumps(body, ensure_ascii=False, default=str)
        except TypeError:
            text = str(body)
    elif hasattr(body, "model_dump"):
        try:
            text = json.dumps(body.model_dump(), ensure_ascii=False, default=str)
        except TypeError:
            text = str(body)
    else:
        text = str(body)
    if not text.strip():
        return "<empty>"
    return text[:limit] + ("...(truncated)" if len(text) > limit else "")


def _capture_google_proxy_request(
    capture: dict[str, Any],
    http_method: Any,
    path: Any,
    request_dict: Any,
    http_options: Any,
) -> None:
    capture.update(
        {
            "http_method": str(http_method).upper() if http_method is not None else None,
            "path": path,
            "request_body": request_dict,
            "http_options": http_options,
        }
    )


def _log_google_proxy_request(model: str, capture: dict[str, Any]) -> None:
    log = logging.getLogger("chatdada.llm")
    log.debug(
        "Gemini proxy request for %s: method=%s path=%s body=%s http_options=%s",
        model,
        capture.get("http_method"),
        capture.get("path"),
        _debug_body_preview(capture.get("request_body")),
        _debug_body_preview(capture.get("http_options")),
    )


def _log_google_proxy_response(model: str, capture: dict[str, Any]) -> None:
    log = logging.getLogger("chatdada.llm")
    log.debug(
        "Gemini proxy response for %s: path=%s response_headers=%s response_body=%s",
        model,
        capture.get("path"),
        capture.get("headers"),
        _debug_body_preview(capture.get("body")),
    )


def _log_google_proxy_failure(model: str, capture: dict[str, Any], exc: Exception) -> None:
    log = logging.getLogger("chatdada.llm")
    log.error(
        (
            "Gemini proxy request failed for %s: method=%s path=%s "
            "request_body=%s http_options=%s response_headers=%s response_body=%s error=%s"
        ),
        model,
        capture.get("http_method"),
        capture.get("path"),
        _debug_body_preview(capture.get("request_body")),
        _debug_body_preview(capture.get("http_options")),
        capture.get("headers"),
        _debug_body_preview(capture.get("body")),
        exc,
    )


def _translate_openai_kwargs_to_gemini(kwargs: dict[str, Any]) -> dict[str, Any]:
    translated = dict(kwargs)

    reasoning_effort = translated.pop("reasoning_effort", None)
    if reasoning_effort is not None and translated.get("thinking_level") is None:
        translated["thinking_level"] = reasoning_effort

    max_tokens = translated.pop("max_tokens", None)
    if max_tokens is not None and translated.get("max_output_tokens") is None:
        translated["max_output_tokens"] = max_tokens

    for key in ("use_responses_api", "output_version"):
        translated.pop(key, None)

    return translated


def _normalize_google_proxy_thinking_level(level: Any) -> str | None:
    normalized = str(level or "").strip().lower()
    if not normalized:
        return None
    if normalized in GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS:
        return normalized
    return "low"


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
    # Keep reasoning out of message.content so downstream summary/rendering paths
    # don't leak <think> traces when using MiniMax's OpenAI-compatible endpoint.
    extra_body.setdefault("reasoning_split", True)
    normalized["extra_body"] = extra_body

    if normalized.get("temperature") is not None:
        normalized["temperature"] = _normalize_minimax_temperature(normalized["temperature"])

    # MiniMax tool-calling on the OpenAI-compatible chat endpoint is safer via
    # non-streaming generation. LangChain may otherwise route ainvoke() through
    # _astream() when tools are bound, which triggers invalid chat settings.
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
    log = logging.getLogger("chatdada.llm")
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


class MiniMaxOpenAIAdapter:
    """Minimal MiniMax chat adapter backed by direct OpenAI SDK calls."""

    use_responses_api = False

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
        self.model = model
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
    def name(self) -> str:
        return str(self.model)

    @property
    def model_name(self) -> str:
        return self.model

    @staticmethod
    def _extract_input_messages(args: tuple[Any, ...]) -> list[Any]:
        if not args or not isinstance(args[0], list):
            raise ValueError("MiniMaxOpenAIAdapter expects a message list as the first positional argument")
        return list(args[0])

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

    def invoke(self, *args: Any, **kwargs: Any) -> AIMessage:
        messages = self._extract_input_messages(args)
        payload = self._build_payload(messages, kwargs)
        response = self._sync_client.chat.completions.create(**payload)
        parsed_payload = response.model_dump(exclude_none=True, mode="json")
        result = _minimax_response_to_ai_message(parsed_payload)
        if getattr(result, "usage_metadata", None) is None:
            log.warning(
                "MiniMax response missing usage for %s: keys=%s response=%s",
                self.model,
                sorted(parsed_payload.keys()),
                _debug_body_preview(parsed_payload),
            )
        return result

    async def ainvoke(self, *args: Any, **kwargs: Any) -> AIMessage:
        messages = self._extract_input_messages(args)
        payload = self._build_payload(messages, kwargs)
        response = await self._async_client.chat.completions.create(**payload)
        parsed_payload = response.model_dump(exclude_none=True, mode="json")
        result = _minimax_response_to_ai_message(parsed_payload)
        if getattr(result, "usage_metadata", None) is None:
            log.warning(
                "MiniMax response missing usage for %s: keys=%s response=%s",
                self.model,
                sorted(parsed_payload.keys()),
                _debug_body_preview(parsed_payload),
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


class GeminiOpenAIAdapter:
    """Adapt our OpenAI-oriented LangChain usage to a Gemini-compatible proxy endpoint."""

    use_responses_api = False

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        llm: Any | None = None,
        **kwargs: Any,
    ) -> None:
        self.model = model
        self._base_url = base_url

        if llm is not None:
            self._llm = llm
            return

        if not api_key:
            raise ValueError("GeminiOpenAIAdapter requires an api_key when llm is not provided")

        from langchain_google_genai import ChatGoogleGenerativeAI

        translated_kwargs = _translate_openai_kwargs_to_gemini(kwargs)
        self._llm = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            **({"base_url": base_url} if base_url else {}),
            **translated_kwargs,
        )

    @property
    def provider(self) -> str:
        return "google"

    @property
    def name(self) -> str:
        return str(self.model)

    @property
    def model_name(self) -> str:
        return self.model

    def _get_sync_api_client(self) -> Any | None:
        return getattr(getattr(getattr(self._llm, "client", None), "models", None), "_api_client", None)

    def _get_async_api_client(self) -> Any | None:
        return getattr(
            getattr(getattr(getattr(self._llm, "client", None), "aio", None), "models", None), "_api_client", None
        )

    @staticmethod
    def _capture_response(capture: dict[str, Any], path: Any, response: Any) -> None:
        capture.update(
            {
                "path": path,
                "headers": dict(getattr(response, "headers", {}) or {}),
                "body": getattr(response, "body", None),
            }
        )

    def _wrap_sync_request(self, api_client: Any, capture: dict[str, Any]) -> tuple[Any | None, Any | None]:
        original_request = getattr(api_client, "request", None)
        if not callable(original_request):
            return None, None

        def wrapped_request(http_method, path, request_dict, http_options=None):
            _capture_google_proxy_request(capture, http_method, path, request_dict, http_options)
            _log_google_proxy_request(self.model, capture)
            response = original_request(http_method, path, request_dict, http_options)
            self._capture_response(capture, path, response)
            _log_google_proxy_response(self.model, capture)
            return response

        return original_request, wrapped_request

    def _wrap_async_request(self, api_client: Any, capture: dict[str, Any]) -> tuple[Any | None, Any | None]:
        original_async_request = getattr(api_client, "async_request", None)
        if not callable(original_async_request):
            return None, None

        async def wrapped_async_request(http_method, path, request_dict, http_options=None):
            _capture_google_proxy_request(capture, http_method, path, request_dict, http_options)
            _log_google_proxy_request(self.model, capture)
            response = await original_async_request(http_method, path, request_dict, http_options)
            self._capture_response(capture, path, response)
            _log_google_proxy_response(self.model, capture)
            return response

        return original_async_request, wrapped_async_request

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        call_kwargs = _translate_openai_kwargs_to_gemini(kwargs)
        api_client = self._get_sync_api_client()
        if api_client is None:
            return self._llm.invoke(*args, **call_kwargs)

        capture: dict[str, Any] = {}
        original_request, wrapped_request = self._wrap_sync_request(api_client, capture)
        if original_request is None or wrapped_request is None:
            return self._llm.invoke(*args, **call_kwargs)

        api_client.request = wrapped_request
        try:
            return self._llm.invoke(*args, **call_kwargs)
        except Exception as exc:
            _log_google_proxy_failure(self.model, capture, exc)
            raise
        finally:
            api_client.request = original_request

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        call_kwargs = _translate_openai_kwargs_to_gemini(kwargs)
        api_client = self._get_async_api_client()
        if api_client is None:
            return await self._llm.ainvoke(*args, **call_kwargs)

        capture: dict[str, Any] = {}
        original_async_request, wrapped_async_request = self._wrap_async_request(api_client, capture)
        if original_async_request is None or wrapped_async_request is None:
            return await self._llm.ainvoke(*args, **call_kwargs)

        api_client.async_request = wrapped_async_request
        try:
            return await self._llm.ainvoke(*args, **call_kwargs)
        except Exception as exc:
            _log_google_proxy_failure(self.model, capture, exc)
            raise
        finally:
            api_client.async_request = original_async_request

    async def astream(self, *args: Any, **kwargs: Any):
        call_kwargs = _translate_openai_kwargs_to_gemini(kwargs)
        api_client = self._get_async_api_client()
        if api_client is None:
            async for chunk in self._llm.astream(*args, **call_kwargs):
                yield chunk
            return

        capture: dict[str, Any] = {}
        original_async_request, wrapped_async_request = self._wrap_async_request(api_client, capture)
        if original_async_request is None or wrapped_async_request is None:
            async for chunk in self._llm.astream(*args, **call_kwargs):
                yield chunk
            return

        api_client.async_request = wrapped_async_request
        try:
            async for chunk in self._llm.astream(*args, **call_kwargs):
                yield chunk
        except Exception as exc:
            _log_google_proxy_failure(self.model, capture, exc)
            raise
        finally:
            api_client.async_request = original_async_request

    def bind_tools(self, *args: Any, **kwargs: Any) -> "GeminiOpenAIAdapter":
        return GeminiOpenAIAdapter(
            self.model,
            base_url=self._base_url,
            llm=self._llm.bind_tools(*args, **kwargs),
        )

    def with_structured_output(self, *args: Any, **kwargs: Any) -> "GeminiOpenAIAdapter":
        return GeminiOpenAIAdapter(
            self.model,
            base_url=self._base_url,
            llm=self._llm.with_structured_output(*args, **kwargs),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)


def _build_client(client_type: str, model: str, api_key: str, **kwargs: Any) -> BaseChatModel:
    """Instantiate the correct LangChain chat model for a given client type."""
    thinking_level = kwargs.pop("thinking_level", None)

    if client_type == "openai":
        base_url = kwargs.pop("base_url", None)
        if thinking_level:
            kwargs["reasoning_effort"] = thinking_level
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            **({"base_url": base_url} if base_url else {}),
            **kwargs,
        )

    if client_type == "minimax_openai":
        base_url = kwargs.pop("base_url", None)
        return MiniMaxOpenAIAdapter(
            model=model,
            api_key=api_key,
            base_url=base_url,
            **kwargs,
        )

    if client_type == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        base_url = kwargs.pop("base_url", None)
        if thinking_level:
            kwargs["thinking_level"] = thinking_level
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            **({"base_url": base_url} if base_url else {}),
            **kwargs,
        )

    if client_type == "gemini_openai_adapter":
        base_url = kwargs.pop("base_url", None)
        if thinking_level:
            kwargs["thinking_level"] = thinking_level
        return GeminiOpenAIAdapter(
            model=model,
            api_key=api_key,
            base_url=base_url,
            **kwargs,
        )

    if client_type == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # Claude uses thinking.budget_tokens; reserved for future extension
        return ChatAnthropic(
            model=model,
            anthropic_api_key=api_key,
            **kwargs,
        )

    raise ValueError(f"Unknown client type '{client_type}'. " f"Add a branch in _build_client() to support it.")


def _normalize_provider_endpoint(client_type: str, endpoint_url: str) -> tuple[str | None, dict]:
    endpoint_url = endpoint_url.rstrip("/")
    extra: dict = {}

    if client_type in {"openai", "minimax_openai"}:
        if endpoint_url.endswith("/v1/responses"):
            return endpoint_url.removesuffix("/responses"), {
                "use_responses_api": True,
                "output_version": "responses/v1",
            }
        return endpoint_url, extra

    if client_type in {"google", "gemini_openai_adapter"}:
        # ChatGoogleGenerativeAI 会自动再拼接默认的 /v1beta，
        # 所以这里必须传不带版本段的基地址，避免出现 /v1beta/v1beta。
        if endpoint_url.endswith("/v1beta"):
            return endpoint_url.removesuffix("/v1beta"), extra
        return endpoint_url, extra

    if client_type == "anthropic":
        if endpoint_url.endswith("/v1/messages"):
            return endpoint_url.removesuffix("/v1/messages"), extra
        return endpoint_url, extra

    return endpoint_url, extra


def response_text(response: Any) -> str:
    """Extract plain text from LangChain responses across chat and responses APIs."""
    if isinstance(response, str):
        return response

    text = getattr(response, "text", None)
    if text is not None:
        return str(text)

    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)

    return str(content)


def build_chat_model(role: str, **kwargs: Any) -> BaseChatModel:
    """Get a raw BaseChatModel instance for a specific agent role.

    Unlike ``get_llm``, this does **not** wrap the result with ``_LoggingLLM``,
    so the returned object is a genuine ``BaseChatModel`` subclass.  Use this
    when a downstream library (e.g. *deepagents*) requires a real
    ``BaseChatModel``.

    Args:
        role: One of the roles defined in MODEL_CONFIGS
        **kwargs: Override any model parameter (e.g. temperature=0, max_tokens=8192)

    Raises:
        KeyError: if role or provider is not registered
        EnvironmentError: if the required API key env var is not set
    """
    if role not in MODEL_CONFIGS:
        raise KeyError(f"Unknown role '{role}'. Registered roles: {list(MODEL_CONFIGS.keys())}")

    config = MODEL_CONFIGS[role].copy()
    provider_name = config.pop("provider")

    if provider_name not in PROVIDERS:
        raise KeyError(f"Unknown provider '{provider_name}'. Available: {list(PROVIDERS.keys())}")

    provider = PROVIDERS[provider_name]
    model = config.pop("model")
    client_kwargs: dict[str, Any] = {}

    api_key_env = provider["api_key_env"]
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise EnvironmentError(
            f"Role '{role}' uses provider '{provider_name}', "
            f"but ${api_key_env} is not set. "
            f"Add it to your .env or shell environment."
        )

    endpoint_url = os.environ.get(provider.get("endpoint_url_env", ""), provider.get("endpoint_url", ""))
    if endpoint_url:
        normalized_base_url, normalized_extra = _normalize_provider_endpoint(provider["client"], endpoint_url)
        if normalized_base_url:
            client_kwargs["base_url"] = normalized_base_url
        client_kwargs.update(normalized_extra)

    # Merge role-level overrides after provider defaults / endpoint normalization
    client_kwargs.update(config)  # remaining role-specific overrides
    client_kwargs.update(kwargs)  # caller overrides win
    client_kwargs.setdefault("timeout", DEFAULT_LLM_TIMEOUT_SECONDS)
    client_kwargs.setdefault("max_retries", DEFAULT_LLM_MAX_RETRIES)
    if provider_name == "minimax":
        client_kwargs = _apply_minimax_defaults(client_kwargs)

    # Inject thinking_level: explicit kwargs > ContextVar > default "medium"
    thinking_level = client_kwargs.pop("thinking_level", None) or _thinking_level.get()
    if provider_name == "google_proxy":
        normalized_thinking_level = _normalize_google_proxy_thinking_level(thinking_level)
        if normalized_thinking_level is not None:
            client_kwargs["thinking_level"] = normalized_thinking_level
    elif provider_name != "minimax":
        client_kwargs["thinking_level"] = thinking_level

    return _build_client(provider["client"], model, api_key, **client_kwargs)


def get_llm(role: str, **kwargs: Any) -> BaseChatModel:
    """Get an LLM instance for a specific agent role (wrapped with logging).

    Args:
        role: One of the roles defined in MODEL_CONFIGS
        **kwargs: Override any model parameter (e.g. temperature=0, max_tokens=8192)

    Raises:
        KeyError: if role or provider is not registered
        EnvironmentError: if the required API key env var is not set
    """
    from core.logger import _LoggingLLM

    model = MODEL_CONFIGS[role]["model"]  # KeyError propagates from build_chat_model
    client = build_chat_model(role, **kwargs)
    return _LoggingLLM(client, role, model)


class _BrowserUseResponsesAdapter:
    """browser_use-compatible adapter over our configured LangChain models."""

    def __init__(self, role: str, model: str, llm: Any, provider: str) -> None:
        self._role = role
        self.model = model
        self._llm = llm
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def name(self) -> str:
        return str(self.model)

    @property
    def model_name(self) -> str:
        return self.model

    async def ainvoke(self, messages: list[Any], output_format: type | None = None, **kwargs: Any):
        from browser_use.llm.views import ChatInvokeCompletion

        langchain_messages = [_browser_use_message_to_langchain(message) for message in messages]
        invoke_kwargs = {k: v for k, v in kwargs.items() if k not in {"session_id", "request_type"}}

        if output_format is None:
            response = await self._llm.ainvoke(langchain_messages, **invoke_kwargs)
            return ChatInvokeCompletion(
                completion=response_text(response),
                usage=None,
                stop_reason=None,
            )

        raw_responses_llm = _unwrap_responses_chat_model(self._llm)
        if raw_responses_llm is not None and hasattr(output_format, "model_json_schema"):
            parsed = await _invoke_structured_via_responses_api(
                raw_responses_llm,
                langchain_messages,
                output_format,
                invoke_kwargs,
            )
        else:
            structured_llm = self._llm.with_structured_output(output_format)
            parsed = await structured_llm.ainvoke(langchain_messages, **invoke_kwargs)
        return ChatInvokeCompletion(
            completion=parsed,
            usage=None,
            stop_reason=None,
        )


def _browser_use_message_to_langchain(message: Any):
    from browser_use.llm.messages import AssistantMessage as BrowserAssistantMessage
    from browser_use.llm.messages import SystemMessage as BrowserSystemMessage
    from browser_use.llm.messages import UserMessage as BrowserUserMessage
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    content = _browser_use_content_to_langchain(getattr(message, "content", ""))

    if isinstance(message, BrowserSystemMessage):
        return SystemMessage(content=content)
    if isinstance(message, BrowserAssistantMessage):
        return AIMessage(content=content)
    if isinstance(message, BrowserUserMessage):
        return HumanMessage(content=content)

    role = str(getattr(message, "role", "user"))
    if role == "system":
        return SystemMessage(content=content)
    if role == "assistant":
        return AIMessage(content=content)
    return HumanMessage(content=content)


def _unwrap_responses_chat_model(llm: Any, max_depth: int = 5) -> Any | None:
    current = llm
    seen: set[int] = set()

    for _ in range(max_depth):
        if current is None:
            return None

        obj_id = id(current)
        if obj_id in seen:
            return None
        seen.add(obj_id)

        if (
            getattr(current, "use_responses_api", False)
            and hasattr(current, "root_async_client")
            and hasattr(current, "_get_request_payload")
        ):
            return current

        current = getattr(current, "_llm", None)

    return None


async def _invoke_structured_via_responses_api(
    llm: Any,
    messages: list[Any],
    output_format: type,
    invoke_kwargs: dict[str, Any],
) -> Any:
    payload = llm._get_request_payload(messages, response_format=output_format, **invoke_kwargs)
    payload.pop("stream", None)
    raw_response = await llm.root_async_client.responses.with_raw_response.parse(**payload)
    parsed_response = raw_response.parse()

    parsed = getattr(parsed_response, "output_parsed", None)
    if parsed is not None:
        return parsed

    for item in getattr(parsed_response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []) or []:
            if getattr(content, "type", None) != "output_text":
                continue
            if getattr(content, "parsed", None) is not None:
                return content.parsed
            text = getattr(content, "text", None)
            if text:
                return output_format.model_validate_json(text)

    raise ValueError(f"Responses API did not return parsed structured output for {output_format.__name__}")


def _browser_use_content_to_langchain(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []

    for part in content:
        part_type = getattr(part, "type", None)
        if part_type == "text":
            text = str(getattr(part, "text", "") or "")
            if text:
                blocks.append({"type": "text", "text": text})
                text_parts.append(text)
        elif part_type == "image_url":
            image_url = getattr(part, "image_url", None)
            url = getattr(image_url, "url", "")
            detail = getattr(image_url, "detail", "auto")
            if url:
                blocks.append({"type": "image_url", "image_url": {"url": url, "detail": detail}})
        elif part_type == "refusal":
            refusal = str(getattr(part, "refusal", "") or "")
            if refusal:
                blocks.append({"type": "text", "text": refusal})
                text_parts.append(refusal)

    if not blocks:
        return ""
    if all(block["type"] == "text" for block in blocks):
        return "\n".join(text_parts)
    return blocks


def get_browser_use_llm(role: str, **kwargs: Any):
    """Get a browser_use-compatible adapter over the role's configured LangChain model."""
    if role not in MODEL_CONFIGS:
        raise KeyError(f"Unknown role '{role}'. Registered roles: {list(MODEL_CONFIGS.keys())}")

    config = MODEL_CONFIGS[role].copy()
    provider_name = config.pop("provider")
    model = config.pop("model")
    llm = get_llm(role, **kwargs)
    provider = _browser_use_provider_name(provider_name)
    return _BrowserUseResponsesAdapter(role, model, llm, provider=provider)


def _browser_use_provider_name(provider_name: str) -> str:
    provider = PROVIDERS.get(provider_name, {})
    client_type = str(provider.get("client", "openai"))
    if client_type in {"google", "gemini_openai_adapter"}:
        return "google"
    if client_type == "anthropic":
        return "anthropic"
    return "openai"
