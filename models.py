"""
Model registry — centralized LLM configuration for all agents.
Each agent role gets its own model config. Change models per-role here.
"""
from langchain_openai import ChatOpenAI


# All model configs in one place. Swap models per role as needed.
MODEL_CONFIGS = {
    "orchestrator": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
    "search": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
    "doc_analyst": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
    "writer": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
    # V2 new roles
    "deep_research": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
    "data_analyst": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
}


def get_llm(role: str, **kwargs) -> ChatOpenAI:
    """Get an LLM instance for a specific agent role.

    Args:
        role: One of "orchestrator", "search", "doc_analyst", "writer"
        **kwargs: Override any config value (e.g. max_tokens=8192)
    """
    config = MODEL_CONFIGS[role].copy()
    config.update(kwargs)
    return ChatOpenAI(
        model=config.pop("model"),
        api_key=config.pop("api_key"),
        base_url=config.pop("base_url"),
        **config,
    )
