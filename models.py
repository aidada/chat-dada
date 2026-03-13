"""
Model registry — centralized LLM configuration for all agents.
Each agent role gets its own model config. Change models per-role here.

Required environment variables (set in .env or shell):
    CO_API_KEY        — for "proxy" provider  (co.yes.vg, handles OpenAI + Gemini via proxy)
    OPENAI_API_KEY    — for "openai" provider (api.openai.com, native)
    MOONSHOT_API_KEY  — for "moonshot" provider (api.moonshot.cn, Kimi native)
    GOOGLE_API_KEY    — for "google" provider  (Gemini native, no proxy)
    ANTHROPIC_API_KEY — for "anthropic" provider (Claude native)

Adding a new provider:
    1. Add an entry to PROVIDERS with client/base_url/api_key_env
    2. Add a branch in _build_client() if it's a new client type
    3. Reference the provider name in MODEL_CONFIGS
"""

import os
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI


# ── Provider definitions ─────────────────────────────────────────────────────
# client: which LangChain class to use. Currently supported:
#   "openai"    → ChatOpenAI  (also works for any OpenAI-compatible API)
#   "google"    → ChatGoogleGenerativeAI (langchain-google-genai, native Gemini)
#   "anthropic" → ChatAnthropic (langchain-anthropic, native Claude)
PROVIDERS: dict[str, dict] = {
    "proxy": {
        "client": "openai",
        "base_url": "https://co.yes.vg",
        "api_key_env": "CO_API_KEY",
    },
    "openai": {
        "client": "openai",
        "base_url": None,  # use SDK default (api.openai.com)
        "api_key_env": "OPENAI_API_KEY",
    },
    "moonshot": {
        "client": "openai",  # Kimi is OpenAI-compatible
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
    },
    "google": {
        "client": "google",  # native Gemini — no base_url needed
        "api_key_env": "GOOGLE_API_KEY",
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
    "search": {"model": "gemini-3.1-pro-preview-customtools", "provider": "proxy"},
    "doc_analyst": {"model": "gpt-5.4", "provider": "proxy"},
    "writer": {"model": "gpt-5.4", "provider": "proxy"},
    "deep_research": {"model": "gemini-3.1-pro-preview-customtools", "provider": "proxy"},
    "data_analyst": {"model": "gemini-3.1-pro-preview-customtools", "provider": "proxy"},
}


def _build_client(client_type: str, model: str, api_key: str, **kwargs: Any) -> BaseChatModel:
    """Instantiate the correct LangChain chat model for a given client type."""
    if client_type == "openai":
        base_url = kwargs.pop("base_url", None)
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            **({"base_url": base_url} if base_url else {}),
            **kwargs,
        )

    if client_type == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            **kwargs,
        )

    if client_type == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model,
            anthropic_api_key=api_key,
            **kwargs,
        )

    raise ValueError(f"Unknown client type '{client_type}'. " f"Add a branch in _build_client() to support it.")


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

    api_key_env = provider["api_key_env"]
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise EnvironmentError(
            f"Role '{role}' uses provider '{provider_name}', "
            f"but ${api_key_env} is not set. "
            f"Add it to your .env or shell environment."
        )

    # Merge provider-level extras (e.g. base_url) then role-level overrides
    client_kwargs: dict[str, Any] = {}
    if "base_url" in provider:
        client_kwargs["base_url"] = provider["base_url"]
    client_kwargs.update(config)  # remaining role-specific overrides
    client_kwargs.update(kwargs)  # caller overrides win

    return _build_client(provider["client"], model, api_key, **client_kwargs)
