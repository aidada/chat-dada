"""Default provider and role-to-model configuration data."""

import os

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
        "client": "openai",
        "endpoint_url": "https://api.moonshot.cn/v1/chat/completions",
        "api_key_env": "MOONSHOT_API_KEY",
    },
    "google_proxy": {
        "client": "gemini_openai_adapter",
        "endpoint_url": "https://co.yes.vg/gemini",
        "endpoint_url_env": "YESCODE_GEMINI_BASE_URL",
        "api_key_env": "CO_API_KEY",
    },
    "anthropic": {
        "client": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
}

MODEL_CONFIGS_BALANCED: dict[str, dict] = {
    "orchestrator": {"model": "gpt-5.4", "provider": "proxy"},
    "search": {"model": "gpt-5.4", "provider": "proxy"},
    "doc_analyst": {"model": "gemini-3.1-pro-preview-customtools", "provider": "google_proxy"},
    "writer": {"model": "gpt-5.4", "provider": "proxy"},
    "deep_research": {"model": "gpt-5.4", "provider": "proxy"},
    "data_analyst": {"model": "gpt-5.4", "provider": "proxy"},
    "research_domain": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
    "patent_domain": {"model": "gpt-5.4", "provider": "proxy"},
    "zero_report_domain": {"model": "gpt-5.4", "provider": "proxy"},
}

MODEL_CONFIGS_ALL_PROXY: dict[str, dict] = {
    "orchestrator": {"model": "gpt-5.4", "provider": "proxy"},
    "search": {"model": "gpt-5.4", "provider": "proxy"},
    "doc_analyst": {"model": "gpt-5.4", "provider": "proxy"},
    "writer": {"model": "gpt-5.4", "provider": "proxy"},
    "deep_research": {"model": "gpt-5.4", "provider": "proxy"},
    "data_analyst": {"model": "gpt-5.4", "provider": "proxy"},
    "research_domain": {"model": "gpt-5.4", "provider": "proxy"},
    "patent_domain": {"model": "gpt-5.4", "provider": "proxy"},
    "zero_report_domain": {"model": "gpt-5.4", "provider": "proxy"},
}

MODEL_CONFIGS_OPENAI_DIRECT: dict[str, dict] = {
    "orchestrator": {"model": "gpt-5.4", "provider": "openai"},
    "search": {"model": "gpt-5.4", "provider": "openai"},
    "doc_analyst": {"model": "gpt-5.4", "provider": "openai"},
    "writer": {"model": "gpt-5.4", "provider": "openai"},
    "deep_research": {"model": "gpt-5.4", "provider": "openai"},
    "data_analyst": {"model": "gpt-5.4", "provider": "openai"},
    "research_domain": {"model": "gpt-5.4", "provider": "openai"},
    "patent_domain": {"model": "gpt-5.4", "provider": "openai"},
    "zero_report_domain": {"model": "gpt-5.4", "provider": "openai"},
}

MODEL_CONFIGS_GOOGLE_RESEARCH: dict[str, dict] = {
    "orchestrator": {"model": "gpt-5.4", "provider": "proxy"},
    "search": {"model": "gemini-3.1-pro-preview", "provider": "google_proxy"},
    "doc_analyst": {"model": "gemini-3.1-pro-preview-customtools", "provider": "google_proxy"},
    "writer": {"model": "gpt-5.4", "provider": "proxy"},
    "deep_research": {"model": "gemini-3.1-pro-preview", "provider": "google_proxy"},
    "data_analyst": {"model": "gemini-3.1-pro-preview-customtools", "provider": "google_proxy"},
    "research_domain": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
    "patent_domain": {"model": "gpt-5.4", "provider": "proxy"},
    "zero_report_domain": {"model": "gpt-5.4", "provider": "proxy"},
}

MODEL_CONFIGS_MINIMAX_RESEARCH: dict[str, dict] = {
    "orchestrator": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
    "search": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
    "doc_analyst": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
    "writer": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
    "deep_research": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
    "data_analyst": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
    "research_domain": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
    "patent_domain": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
    "zero_report_domain": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
}

MODEL_CONFIG_PRESETS: dict[str, dict[str, dict]] = {
    "balanced": MODEL_CONFIGS_BALANCED,
    "all_proxy": MODEL_CONFIGS_ALL_PROXY,
    "openai_direct": MODEL_CONFIGS_OPENAI_DIRECT,
    "google_research": MODEL_CONFIGS_GOOGLE_RESEARCH,
    "minimax_research": MODEL_CONFIGS_MINIMAX_RESEARCH,
}

# Switch by setting MODEL_CONFIG_PRESET in env, or edit the fallback name below.
ACTIVE_MODEL_CONFIG_PRESET = (os.environ.get("MODEL_CONFIG_PRESET") or "balanced").strip() or "balanced"
if ACTIVE_MODEL_CONFIG_PRESET not in MODEL_CONFIG_PRESETS:
    raise ValueError(
        f"Unknown MODEL_CONFIG_PRESET '{ACTIVE_MODEL_CONFIG_PRESET}'. "
        f"Available presets: {list(MODEL_CONFIG_PRESETS.keys())}"
    )

MODEL_CONFIGS: dict[str, dict] = {
    role: dict(config)
    for role, config in MODEL_CONFIG_PRESETS[ACTIVE_MODEL_CONFIG_PRESET].items()
}

DEFAULT_LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "7200"))
DEFAULT_LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "2"))
GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS = {"low", "high"}
