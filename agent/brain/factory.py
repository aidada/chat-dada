"""LLM client factory for agent roles."""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from agent.brain.defaults import DEFAULT_LLM_MAX_RETRIES, DEFAULT_LLM_TIMEOUT_SECONDS
from agent.brain.providers.browser_use import (
    BrowserUseResponsesAdapter,
    browser_use_provider_name,
)
from agent.brain.providers.deepseek import DeepSeekOpenAIAdapter
from agent.brain.providers.gemini import GeminiOpenAIAdapter
from agent.brain.providers.minimax import MiniMaxOpenAIAdapter, _apply_minimax_defaults
from agent.brain.registry import registry
from agent.brain.thinking import (
    get_thinking_level,
    normalize_google_proxy_thinking_level,
)

log = logging.getLogger("chatdada.llm")


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

    if client_type == "deepseek_openai":
        base_url = kwargs.pop("base_url", None)
        return DeepSeekOpenAIAdapter(
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

        return ChatAnthropic(
            model=model,
            anthropic_api_key=api_key,
            **kwargs,
        )

    raise ValueError(f"Unknown client type '{client_type}'. Add a branch in _build_client() to support it.")


def _normalize_provider_endpoint(client_type: str, endpoint_url: str) -> tuple[str | None, dict]:
    """Normalize a provider endpoint URL for the given client type."""
    endpoint_url = endpoint_url.rstrip("/")
    extra: dict[str, Any] = {}

    if client_type in {"openai", "minimax_openai", "deepseek_openai"}:
        if endpoint_url.endswith("/v1/responses"):
            return endpoint_url.removesuffix("/responses"), {
                "use_responses_api": True,
                "output_version": "responses/v1",
            }
        return endpoint_url, extra

    if client_type in {"google", "gemini_openai_adapter"}:
        if endpoint_url.endswith("/v1beta"):
            return endpoint_url.removesuffix("/v1beta"), extra
        return endpoint_url, extra

    if client_type == "anthropic":
        if endpoint_url.endswith("/v1/messages"):
            return endpoint_url.removesuffix("/v1/messages"), extra
        return endpoint_url, extra

    return endpoint_url, extra


def build_chat_model(
    role: str,
    *,
    task_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """Get a raw BaseChatModel instance for a specific agent role."""
    spec = registry.get(role, task_context=task_context)
    client_kwargs: dict[str, Any] = {}
    api_key = os.environ.get(spec.api_key_env)
    if not api_key:
        raise EnvironmentError(
            f"Role '{role}' uses provider '{spec.provider}', "
            f"but ${spec.api_key_env} is not set. "
            "Add it to your .env or shell environment."
        )

    if spec.endpoint_url:
        normalized_base_url, normalized_extra = _normalize_provider_endpoint(
            spec.client_type,
            spec.endpoint_url,
        )
        if normalized_base_url:
            client_kwargs["base_url"] = normalized_base_url
        client_kwargs.update(normalized_extra)

    client_kwargs.update(spec.overrides)
    client_kwargs.update(kwargs)
    client_kwargs.setdefault("timeout", DEFAULT_LLM_TIMEOUT_SECONDS)
    client_kwargs.setdefault("max_retries", DEFAULT_LLM_MAX_RETRIES)
    if spec.provider == "minimax":
        client_kwargs = _apply_minimax_defaults(client_kwargs)

    thinking_level = client_kwargs.pop("thinking_level", None) or get_thinking_level()
    if spec.provider == "google_proxy":
        normalized_thinking_level = normalize_google_proxy_thinking_level(thinking_level)
        if normalized_thinking_level is not None:
            client_kwargs["thinking_level"] = normalized_thinking_level
    elif spec.provider != "minimax":
        client_kwargs["thinking_level"] = thinking_level

    return _build_client(spec.client_type, spec.model, api_key, **client_kwargs)


def get_llm(
    role: str,
    *,
    task_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """Get an LLM instance for a specific agent role wrapped with logging."""
    from core.logger import _LoggingLLM

    spec = registry.get(role, task_context=task_context)
    client = build_chat_model(role, task_context=task_context, **kwargs)
    return _LoggingLLM(client, role, spec.model)


def get_browser_use_llm(role: str, **kwargs: Any) -> BrowserUseResponsesAdapter:
    """Get a browser_use-compatible adapter over the role's configured LangChain model."""
    spec = registry.get(role)
    llm = get_llm(role, **kwargs)
    provider = browser_use_provider_name(spec.provider)
    return BrowserUseResponsesAdapter(role, spec.model, llm, provider=provider)
