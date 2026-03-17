"""
Model registry — centralized LLM configuration for all agents.
Each agent role gets its own model config. Change models per-role here.

Required environment variables (set in .env or shell):
    CO_API_KEY        — for "proxy" provider  (co.yes.vg, handles OpenAI + Gemini via proxy)
    OPENAI_API_KEY    — for "openai" provider (api.openai.com, native)
    MOONSHOT_API_KEY  — for "moonshot" provider (api.moonshot.cn, Kimi native)
    GOOGLE_API_KEY    — for "google" provider  (Gemini native, no proxy)
    ANTHROPIC_API_KEY — for "anthropic" provider (Claude native)
Optional environment variables:
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
from langchain_openai import ChatOpenAI

# Current request's thinking level, set by the WebSocket layer
_thinking_level: ContextVar[str] = ContextVar("thinking_level", default="medium")
DEFAULT_LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "7200"))
DEFAULT_LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "2"))
GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS = {"low", "high"}


def set_thinking_level(level: str) -> None:
    """Set thinking level for current async context. Called from WebSocket handler."""
    _thinking_level.set(level)


# ── Provider definitions ─────────────────────────────────────────────────────
# client: which LangChain class to use. Currently supported:
#   "openai"    → ChatOpenAI  (also works for any OpenAI-compatible API)
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
    "deep_research": {"model": "gemini-3.1-pro-preview-customtools", "provider": "google_proxy"},
    "data_analyst": {"model": "gpt-5.4", "provider": "proxy"},
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
        return getattr(getattr(getattr(getattr(self._llm, "client", None), "aio", None), "models", None), "_api_client", None)

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

    if client_type == "openai":
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


def get_llm(role: str, **kwargs: Any) -> BaseChatModel:
    """Get an LLM instance for a specific agent role.

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

    # Inject thinking_level: explicit kwargs > ContextVar > default "medium"
    thinking_level = client_kwargs.pop("thinking_level", None) or _thinking_level.get()
    if provider_name == "google_proxy":
        normalized_thinking_level = _normalize_google_proxy_thinking_level(thinking_level)
        if normalized_thinking_level is not None:
            client_kwargs["thinking_level"] = normalized_thinking_level
    else:
        client_kwargs["thinking_level"] = thinking_level

    from logger import _LoggingLLM

    client = _build_client(provider["client"], model, api_key, **client_kwargs)
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
