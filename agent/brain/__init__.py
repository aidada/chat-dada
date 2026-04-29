"""Brain LLM Layer - modular, runtime-switchable LLM configuration."""

from agent.brain.context import (
    clear_task_model_override,
    get_task_model_override,
    set_task_model_override,
)
from agent.brain.defaults import (
    DEFAULT_LLM_MAX_RETRIES,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS,
    MODEL_CONFIGS,
    PROVIDERS,
)
from agent.brain.factory import (
    build_chat_model,
    get_browser_use_llm,
    get_llm,
    response_text,
)
from agent.brain.providers import (
    BrowserUseResponsesAdapter,
    DeepSeekOpenAIAdapter,
    GeminiOpenAIAdapter,
    MiniMaxOpenAIAdapter,
)
from agent.brain.registry import ModelRegistry, ModelSpec, registry
from agent.brain.thinking import (
    get_thinking_level,
    normalize_google_proxy_thinking_level,
    set_thinking_level,
)

__all__ = [
    "get_llm",
    "build_chat_model",
    "get_browser_use_llm",
    "response_text",
    "registry",
    "ModelRegistry",
    "ModelSpec",
    "set_thinking_level",
    "get_thinking_level",
    "set_task_model_override",
    "get_task_model_override",
    "clear_task_model_override",
    "PROVIDERS",
    "MODEL_CONFIGS",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "DEFAULT_LLM_MAX_RETRIES",
    "GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS",
    "DeepSeekOpenAIAdapter",
    "MiniMaxOpenAIAdapter",
    "GeminiOpenAIAdapter",
    "BrowserUseResponsesAdapter",
    "normalize_google_proxy_thinking_level",
]
