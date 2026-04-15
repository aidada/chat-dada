"""Backward-compatibility re-export shim for the brain LLM layer."""

from openai import AsyncOpenAI, OpenAI

from agent.brain import *  # noqa: F401, F403
from agent.brain.factory import _build_client, _normalize_provider_endpoint  # noqa: F401
from agent.brain.providers.minimax import _apply_minimax_defaults  # noqa: F401
from agent.brain.registry import registry
from agent.brain.thinking import (  # noqa: F401
    normalize_google_proxy_thinking_level as _normalize_google_proxy_thinking_level,
)

# Keep the legacy patch target wired to the live runtime registry config.
MODEL_CONFIGS = registry._configs
