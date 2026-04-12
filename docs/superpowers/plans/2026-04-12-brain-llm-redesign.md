# Brain LLM Layer Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `core/models.py` (1316 lines) into `agent/brain/` package with ModelRegistry for runtime hot-swap and per-task Coordinator routing.

**Architecture:** Extract the monolith into 7 focused modules under `agent/brain/`, introduce a `ModelRegistry` singleton for runtime-configurable role→model mapping with a 4-level priority chain (task_context > ContextVar > registry > defaults), and add Admin API endpoints for hot-swap. Existing imports via `core.models` continue to work through a thin re-export shim.

**Tech Stack:** Python 3.13, LangChain, FastAPI, threading.Lock, contextvars

---

## File Structure

```
agent/brain/                    # NEW package
├── __init__.py                 # Public API: get_llm, build_chat_model, registry, etc.
├── defaults.py                 # PROVIDERS + MODEL_CONFIGS dicts (pure data, ~50 lines)
├── thinking.py                 # _thinking_level ContextVar + normalization (~30 lines)
├── context.py                  # _task_model_override ContextVar (~25 lines)
├── registry.py                 # ModelRegistry singleton (~120 lines)
├── factory.py                  # _build_client, _normalize_provider_endpoint, public factory functions (~200 lines)
└── providers/
    ├── __init__.py             # Re-exports adapter classes
    ├── _utils.py               # Shared helpers: _debug_body_preview (~25 lines)
    ├── minimax.py              # MiniMaxOpenAIAdapter + all MiniMax helpers (~450 lines)
    ├── gemini.py               # GeminiOpenAIAdapter + Gemini helpers (~200 lines)
    └── browser_use.py          # _BrowserUseResponsesAdapter + helpers (~140 lines)

core/models.py                  # MODIFIED → thin re-export shim (~15 lines)
agent/coordinator/state.py      # MODIFIED → add model_hints field to CoordinatorState
agent/coordinator/agent.py      # MODIFIED → output model_hints from understand_goal_node
web/routers/system.py           # MODIFIED → add admin model management endpoints
tests/test_brain_registry.py    # NEW — ModelRegistry unit tests
```

---

### Task 1: Create `agent/brain/defaults.py` — Pure Data

**Files:**
- Create: `agent/brain/__init__.py` (empty for now, enables package import)
- Create: `agent/brain/defaults.py`

- [ ] **Step 1: Create package init and defaults module**

```python
# agent/brain/__init__.py
# Public API — populated in Task 9
```

```python
# agent/brain/defaults.py
"""
Default provider and role-to-model configuration data.

This module is pure data — no logic, no imports beyond stdlib.
Edit PROVIDERS to add/remove LLM providers.
Edit MODEL_CONFIGS to change which model each agent role uses by default.
"""

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

MODEL_CONFIGS: dict[str, dict] = {
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

DEFAULT_LLM_TIMEOUT_SECONDS = 7200
DEFAULT_LLM_MAX_RETRIES = 2
GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS = {"low", "high"}
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -c "from agent.brain.defaults import PROVIDERS, MODEL_CONFIGS; print(len(PROVIDERS), len(MODEL_CONFIGS))"`

Expected: `6 9`

- [ ] **Step 3: Commit**

```bash
git add agent/brain/__init__.py agent/brain/defaults.py
git commit -m "feat(brain): add defaults module with provider and model config data"
```

---

### Task 2: Create `agent/brain/thinking.py` — Thinking Level ContextVar

**Files:**
- Create: `agent/brain/thinking.py`

- [ ] **Step 1: Create thinking module**

Extract the thinking_level ContextVar and its helpers from `core/models.py` lines 37, 40, 44-47, 203-209.

```python
# agent/brain/thinking.py
"""Thinking-level management via ContextVar — async-safe per-request control."""

import os
from contextvars import ContextVar
from typing import Any

from agent.brain.defaults import GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS

_thinking_level: ContextVar[str] = ContextVar("thinking_level", default="medium")


def set_thinking_level(level: str) -> None:
    """Set thinking level for current async context. Called from WebSocket handler."""
    _thinking_level.set(level)


def get_thinking_level() -> str:
    """Return the thinking level for the current async context."""
    return _thinking_level.get()


def normalize_google_proxy_thinking_level(level: Any) -> str | None:
    """Normalize a thinking level value for the Google proxy provider.

    Returns 'low' or 'high' if valid, 'low' for unrecognized non-empty values, None if empty.
    """
    normalized = str(level or "").strip().lower()
    if not normalized:
        return None
    if normalized in GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS:
        return normalized
    return "low"
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -c "from agent.brain.thinking import set_thinking_level, get_thinking_level, normalize_google_proxy_thinking_level; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agent/brain/thinking.py
git commit -m "feat(brain): add thinking-level ContextVar module"
```

---

### Task 3: Create `agent/brain/context.py` — Task Model Override ContextVar

**Files:**
- Create: `agent/brain/context.py`

- [ ] **Step 1: Create context module**

```python
# agent/brain/context.py
"""Per-task model override via ContextVar.

Allows Coordinator or any caller to set model overrides that apply
only within the current async context (i.e., one task execution).

The override dict is keyed by role name, e.g.:
    {"doc_analyst": {"model": "gemini-2.5-pro"}, "search": {"provider": "openai"}}
"""

from contextvars import ContextVar
from typing import Any

_task_model_override: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(
    "task_model_override", default=None
)


def set_task_model_override(overrides: dict[str, dict[str, Any]]) -> None:
    """Set per-task model overrides keyed by role for the current async context.

    Args:
        overrides: dict mapping role names to override dicts.
                   e.g. {"doc_analyst": {"model": "gemini-2.5-pro"}}
    """
    _task_model_override.set(overrides)


def get_task_model_override() -> dict[str, dict[str, Any]] | None:
    """Return the per-task model overrides (role-keyed), or None if not set."""
    return _task_model_override.get()


def clear_task_model_override() -> None:
    """Clear per-task model overrides for the current async context."""
    _task_model_override.set(None)
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -c "from agent.brain.context import set_task_model_override, get_task_model_override; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agent/brain/context.py
git commit -m "feat(brain): add per-task model override ContextVar"
```

---

### Task 4: Create `agent/brain/providers/` — Move MiniMax Adapter

**Files:**
- Create: `agent/brain/providers/__init__.py`
- Create: `agent/brain/providers/_utils.py`
- Create: `agent/brain/providers/minimax.py`

- [ ] **Step 1: Create providers package and shared utils**

```python
# agent/brain/providers/__init__.py
"""LLM provider adapters."""

from agent.brain.providers.minimax import MiniMaxOpenAIAdapter
from agent.brain.providers.gemini import GeminiOpenAIAdapter
from agent.brain.providers.browser_use import BrowserUseResponsesAdapter

__all__ = ["MiniMaxOpenAIAdapter", "GeminiOpenAIAdapter", "BrowserUseResponsesAdapter"]
```

Note: this file will cause import errors until Tasks 5-6 create the gemini and browser_use modules. Create a temporary version that only imports minimax for now:

```python
# agent/brain/providers/__init__.py (temporary — Tasks 5-6 will add remaining imports)
"""LLM provider adapters."""
```

```python
# agent/brain/providers/_utils.py
"""Shared helpers for provider adapters."""

import json
from typing import Any


def debug_body_preview(body: Any, limit: int = 4000) -> str:
    """Format a request/response body for debug logging."""
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
```

- [ ] **Step 2: Move MiniMaxOpenAIAdapter**

Create `agent/brain/providers/minimax.py` by moving the following code from `core/models.py`:
- `_normalize_minimax_temperature` (lines 212-219)
- `_apply_minimax_defaults` (lines 222-239)
- `_find_wrapped_chat_openai` (lines 242-267) — NOTE: unused after checking usages. Skip if not referenced.
- `_message_reasoning_details` (lines 270-275)
- `_merge_reasoning_details_into_payload` (lines 278-300)
- `_collapse_minimax_system_messages` (lines 303-337)
- `_log_minimax_payload_summary` (lines 340-363)
- `_minimax_usage_metadata` (lines 366-385)
- `_reasoning_text_from_details` (lines 388-412)
- `_message_content_to_minimax_text` (lines 415-428)
- `_langchain_message_to_minimax_dict` (lines 431-474)
- `_minimax_response_to_ai_message` (lines 477-546)
- `_parse_structured_output` (lines 549-552)
- `_MiniMaxStructuredOutputAdapter` (lines 555-567)
- `MiniMaxOpenAIAdapter` (lines 569-753)

Copy all of these functions/classes into `agent/brain/providers/minimax.py`. The file header:

```python
# agent/brain/providers/minimax.py
"""MiniMax OpenAI-compatible adapter.

Provides MiniMaxOpenAIAdapter — a custom chat client that talks to MiniMax's
OpenAI-compatible endpoint with reasoning_split support.
"""

import json
import logging
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.utils.function_calling import convert_to_openai_tool
from openai import AsyncOpenAI, OpenAI

from agent.brain.providers._utils import debug_body_preview

log = logging.getLogger("chatdada.llm")

# Then paste all the functions and classes listed above, in order.
# Replace _debug_body_preview → debug_body_preview (imported from _utils).
# No other changes needed — the functions are self-contained.
```

The file should end with:

```python
__all__ = ["MiniMaxOpenAIAdapter"]
```

- [ ] **Step 3: Verify MiniMax adapter imports correctly**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -c "from agent.brain.providers.minimax import MiniMaxOpenAIAdapter; print('OK')"`

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add agent/brain/providers/
git commit -m "feat(brain): move MiniMax adapter to agent/brain/providers/"
```

---

### Task 5: Move Gemini Adapter to `agent/brain/providers/gemini.py`

**Files:**
- Create: `agent/brain/providers/gemini.py`

- [ ] **Step 1: Create Gemini adapter module**

Move the following from `core/models.py`:
- `_capture_google_proxy_request` (lines 128-142)
- `_log_google_proxy_request` (lines 145-154)
- `_log_google_proxy_response` (lines 157-165)
- `_log_google_proxy_failure` (lines 168-183)
- `_translate_openai_kwargs_to_gemini` (lines 186-200)
- `GeminiOpenAIAdapter` (lines 756-931)

```python
# agent/brain/providers/gemini.py
"""Gemini OpenAI adapter for Google AI proxy.

Wraps ChatGoogleGenerativeAI with request/response logging and
OpenAI-to-Gemini parameter translation.
"""

import logging
from typing import Any

from agent.brain.providers._utils import debug_body_preview

log = logging.getLogger("chatdada.llm")


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
    log.debug(
        "Gemini proxy request for %s: method=%s path=%s body=%s http_options=%s",
        model,
        capture.get("http_method"),
        capture.get("path"),
        debug_body_preview(capture.get("request_body")),
        debug_body_preview(capture.get("http_options")),
    )


def _log_google_proxy_response(model: str, capture: dict[str, Any]) -> None:
    log.debug(
        "Gemini proxy response for %s: path=%s response_headers=%s response_body=%s",
        model,
        capture.get("path"),
        capture.get("headers"),
        debug_body_preview(capture.get("body")),
    )


def _log_google_proxy_failure(model: str, capture: dict[str, Any], exc: Exception) -> None:
    log.error(
        (
            "Gemini proxy request failed for %s: method=%s path=%s "
            "request_body=%s http_options=%s response_headers=%s response_body=%s error=%s"
        ),
        model,
        capture.get("http_method"),
        capture.get("path"),
        debug_body_preview(capture.get("request_body")),
        debug_body_preview(capture.get("http_options")),
        capture.get("headers"),
        debug_body_preview(capture.get("body")),
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


class GeminiOpenAIAdapter:
    """Adapt our OpenAI-oriented LangChain usage to a Gemini-compatible proxy endpoint."""

    use_responses_api = False

    # ... (copy the entire class body from core/models.py lines 761-931 verbatim)
    # No changes needed — just move the code.

__all__ = ["GeminiOpenAIAdapter"]
```

**Important**: Copy the `GeminiOpenAIAdapter` class body exactly from `core/models.py:761-931`. The only change is replacing `_debug_body_preview` calls in the `_log_*` helper functions with `debug_body_preview` (already done above since the helpers are rewritten).

- [ ] **Step 2: Verify import**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -c "from agent.brain.providers.gemini import GeminiOpenAIAdapter; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agent/brain/providers/gemini.py
git commit -m "feat(brain): move Gemini adapter to agent/brain/providers/"
```

---

### Task 6: Move BrowserUse Adapter to `agent/brain/providers/browser_use.py`

**Files:**
- Create: `agent/brain/providers/browser_use.py`

- [ ] **Step 1: Create BrowserUse adapter module**

Move the following from `core/models.py`:
- `_BrowserUseResponsesAdapter` (lines 1129-1179)
- `_browser_use_message_to_langchain` (lines 1182-1202)
- `_unwrap_responses_chat_model` (lines 1205-1227)
- `_invoke_structured_via_responses_api` (lines 1230-1257)
- `_browser_use_content_to_langchain` (lines 1260-1292)
- `_browser_use_provider_name` (lines 1308-1315)

```python
# agent/brain/providers/browser_use.py
"""browser_use-compatible adapter over LangChain models.

Bridges our role-based LLM factory with the browser_use SDK's
expected interface (ainvoke with output_format support).
"""

from typing import Any


def response_text(response: Any) -> str:
    """Extract plain text — local copy to avoid circular import with factory."""
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


# ... (copy _unwrap_responses_chat_model, _invoke_structured_via_responses_api,
#      _browser_use_message_to_langchain, _browser_use_content_to_langchain,
#      _browser_use_provider_name from core/models.py verbatim)


class BrowserUseResponsesAdapter:
    """browser_use-compatible adapter over our configured LangChain models."""
    # ... (copy from core/models.py lines 1129-1179, renamed from _BrowserUseResponsesAdapter)
    # Uses the local response_text() defined above.


__all__ = ["BrowserUseResponsesAdapter", "browser_use_provider_name"]
```

**Note**: Rename `_BrowserUseResponsesAdapter` → `BrowserUseResponsesAdapter` (public within the package). Rename `_browser_use_provider_name` → `browser_use_provider_name`.

- [ ] **Step 2: Update providers `__init__.py`**

Now that all three adapter modules exist, set the final `agent/brain/providers/__init__.py`:

```python
# agent/brain/providers/__init__.py
"""LLM provider adapters."""

from agent.brain.providers.minimax import MiniMaxOpenAIAdapter
from agent.brain.providers.gemini import GeminiOpenAIAdapter
from agent.brain.providers.browser_use import BrowserUseResponsesAdapter

__all__ = ["MiniMaxOpenAIAdapter", "GeminiOpenAIAdapter", "BrowserUseResponsesAdapter"]
```

- [ ] **Step 3: Verify all providers import**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -c "from agent.brain.providers import MiniMaxOpenAIAdapter, GeminiOpenAIAdapter, BrowserUseResponsesAdapter; print('OK')"`

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add agent/brain/providers/
git commit -m "feat(brain): move BrowserUse adapter and finalize providers package"
```

---

### Task 7: Create `agent/brain/factory.py` — Client Factory + Public Functions

**Files:**
- Create: `agent/brain/factory.py`

- [ ] **Step 1: Create factory module**

This module contains `_build_client`, `_normalize_provider_endpoint`, `response_text`, `build_chat_model`, `get_llm`, and `get_browser_use_llm`. These are the current public API of `core/models.py`.

```python
# agent/brain/factory.py
"""LLM client factory — builds configured LangChain models for agent roles.

Public functions:
    build_chat_model(role, **kwargs) → raw BaseChatModel
    get_llm(role, **kwargs) → _LoggingLLM-wrapped model
    get_browser_use_llm(role, **kwargs) → BrowserUseResponsesAdapter
    response_text(response) → str
"""

import logging
import os
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from agent.brain.defaults import DEFAULT_LLM_MAX_RETRIES, DEFAULT_LLM_TIMEOUT_SECONDS
from agent.brain.providers.gemini import GeminiOpenAIAdapter
from agent.brain.providers.minimax import MiniMaxOpenAIAdapter, _apply_minimax_defaults
from agent.brain.thinking import get_thinking_level, normalize_google_proxy_thinking_level

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
    """Normalize a provider endpoint URL for the given client type.

    Returns (base_url, extra_kwargs) tuple.
    """
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
        if endpoint_url.endswith("/v1beta"):
            return endpoint_url.removesuffix("/v1beta"), extra
        return endpoint_url, extra

    if client_type == "anthropic":
        if endpoint_url.endswith("/v1/messages"):
            return endpoint_url.removesuffix("/v1/messages"), extra
        return endpoint_url, extra

    return endpoint_url, extra


def build_chat_model(role: str, *, task_context: dict[str, Any] | None = None, **kwargs: Any) -> BaseChatModel:
    """Get a raw BaseChatModel instance for a specific agent role.

    Unlike ``get_llm``, this does **not** wrap the result with ``_LoggingLLM``.

    Args:
        role: One of the roles defined in MODEL_CONFIGS
        task_context: Optional per-task model overrides from Coordinator routing
        **kwargs: Override any model parameter (e.g. temperature=0, max_tokens=8192)
    """
    from agent.brain.registry import registry

    spec = registry.get(role, task_context=task_context)
    provider = spec.provider_config
    client_kwargs: dict[str, Any] = {}

    api_key = os.environ.get(spec.api_key_env)
    if not api_key:
        raise EnvironmentError(
            f"Role '{role}' uses provider '{spec.provider}', "
            f"but ${spec.api_key_env} is not set. "
            f"Add it to your .env or shell environment."
        )

    if spec.endpoint_url:
        normalized_base_url, normalized_extra = _normalize_provider_endpoint(spec.client_type, spec.endpoint_url)
        if normalized_base_url:
            client_kwargs["base_url"] = normalized_base_url
        client_kwargs.update(normalized_extra)

    # Merge overrides: spec defaults < caller kwargs
    client_kwargs.update(spec.overrides)
    client_kwargs.update(kwargs)
    client_kwargs.setdefault("timeout", DEFAULT_LLM_TIMEOUT_SECONDS)
    client_kwargs.setdefault("max_retries", DEFAULT_LLM_MAX_RETRIES)
    if spec.provider == "minimax":
        client_kwargs = _apply_minimax_defaults(client_kwargs)

    # Inject thinking_level
    thinking_level = client_kwargs.pop("thinking_level", None) or get_thinking_level()
    if spec.provider == "google_proxy":
        normalized_thinking_level = normalize_google_proxy_thinking_level(thinking_level)
        if normalized_thinking_level is not None:
            client_kwargs["thinking_level"] = normalized_thinking_level
    elif spec.provider != "minimax":
        client_kwargs["thinking_level"] = thinking_level

    return _build_client(spec.client_type, spec.model, api_key, **client_kwargs)


def get_llm(role: str, *, task_context: dict[str, Any] | None = None, **kwargs: Any) -> BaseChatModel:
    """Get an LLM instance for a specific agent role (wrapped with logging).

    Args:
        role: One of the roles defined in MODEL_CONFIGS
        task_context: Optional per-task model overrides from Coordinator routing
        **kwargs: Override any model parameter
    """
    from core.logger import _LoggingLLM
    from agent.brain.registry import registry

    spec = registry.get(role, task_context=task_context)
    client = build_chat_model(role, task_context=task_context, **kwargs)
    return _LoggingLLM(client, role, spec.model)


def get_browser_use_llm(role: str, **kwargs: Any):
    """Get a browser_use-compatible adapter over the role's configured LangChain model."""
    from agent.brain.providers.browser_use import BrowserUseResponsesAdapter, browser_use_provider_name
    from agent.brain.registry import registry

    spec = registry.get(role)
    llm = get_llm(role, **kwargs)
    provider = browser_use_provider_name(spec.provider)
    return BrowserUseResponsesAdapter(role, spec.model, llm, provider=provider)
```

- [ ] **Step 2: Verify factory module imports (will fail until registry exists)**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -c "from agent.brain.factory import response_text; print(response_text('hello'))"`

Expected: `hello`

- [ ] **Step 3: Commit**

```bash
git add agent/brain/factory.py
git commit -m "feat(brain): add factory module with build_chat_model, get_llm, response_text"
```

---

### Task 8: Create `agent/brain/registry.py` — ModelRegistry Singleton

**Files:**
- Create: `agent/brain/registry.py`
- Test: `tests/test_brain_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_brain_registry.py
"""Tests for agent.brain.registry.ModelRegistry."""

from __future__ import annotations

import threading
import unittest

from agent.brain.registry import ModelRegistry, ModelSpec


class TestModelRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ModelRegistry()

    def test_get_returns_default_config(self) -> None:
        spec = self.registry.get("orchestrator")
        self.assertIsInstance(spec, ModelSpec)
        self.assertEqual(spec.role, "orchestrator")
        self.assertEqual(spec.model, "gpt-5.4")
        self.assertEqual(spec.provider, "proxy")
        self.assertEqual(spec.client_type, "openai")
        self.assertEqual(spec.api_key_env, "CO_API_KEY")

    def test_get_unknown_role_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.registry.get("nonexistent_role")

    def test_update_changes_model(self) -> None:
        self.registry.update("orchestrator", model="gpt-6")
        spec = self.registry.get("orchestrator")
        self.assertEqual(spec.model, "gpt-6")
        self.assertEqual(spec.provider, "proxy")  # unchanged

    def test_update_changes_provider(self) -> None:
        self.registry.update("orchestrator", provider="openai")
        spec = self.registry.get("orchestrator")
        self.assertEqual(spec.provider, "openai")
        self.assertEqual(spec.client_type, "openai")
        self.assertEqual(spec.api_key_env, "OPENAI_API_KEY")

    def test_update_unknown_provider_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.registry.update("orchestrator", provider="nonexistent")

    def test_bulk_update(self) -> None:
        self.registry.bulk_update({
            "orchestrator": {"model": "gpt-6"},
            "search": {"model": "gpt-6"},
        })
        self.assertEqual(self.registry.get("orchestrator").model, "gpt-6")
        self.assertEqual(self.registry.get("search").model, "gpt-6")

    def test_bulk_update_atomic_on_failure(self) -> None:
        """If one role has an invalid provider, none should be updated."""
        with self.assertRaises(KeyError):
            self.registry.bulk_update({
                "orchestrator": {"model": "gpt-6"},
                "search": {"provider": "nonexistent"},
            })
        # orchestrator should NOT have been updated
        self.assertEqual(self.registry.get("orchestrator").model, "gpt-5.4")

    def test_reset_restores_defaults(self) -> None:
        self.registry.update("orchestrator", model="gpt-6")
        self.registry.reset()
        self.assertEqual(self.registry.get("orchestrator").model, "gpt-5.4")

    def test_snapshot_returns_all_roles(self) -> None:
        snap = self.registry.snapshot()
        self.assertIn("orchestrator", snap)
        self.assertIn("search", snap)
        self.assertEqual(len(snap), 9)  # 9 roles in MODEL_CONFIGS

    def test_task_context_overrides_registry(self) -> None:
        spec = self.registry.get("orchestrator", task_context={"model": "gpt-6"})
        self.assertEqual(spec.model, "gpt-6")
        self.assertEqual(spec.provider, "proxy")  # not overridden

    def test_task_context_overrides_provider(self) -> None:
        spec = self.registry.get("orchestrator", task_context={"provider": "openai"})
        self.assertEqual(spec.provider, "openai")
        self.assertEqual(spec.api_key_env, "OPENAI_API_KEY")

    def test_contextvar_override_role_keyed(self) -> None:
        """ContextVar overrides are keyed by role name."""
        from agent.brain.context import clear_task_model_override, set_task_model_override

        set_task_model_override({"orchestrator": {"model": "gpt-6"}, "search": {"provider": "openai"}})
        try:
            spec = self.registry.get("orchestrator")
            self.assertEqual(spec.model, "gpt-6")  # overridden
            spec2 = self.registry.get("search")
            self.assertEqual(spec2.provider, "openai")  # overridden
            spec3 = self.registry.get("writer")
            self.assertEqual(spec3.model, "gpt-5.4")  # NOT overridden
        finally:
            clear_task_model_override()

    def test_concurrent_updates(self) -> None:
        """Multiple threads updating the registry should not corrupt state."""
        errors: list[Exception] = []

        def update_loop(model: str) -> None:
            try:
                for _ in range(100):
                    self.registry.update("orchestrator", model=model)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=update_loop, args=(f"model-{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        # Final state should be one of the models
        spec = self.registry.get("orchestrator")
        self.assertTrue(spec.model.startswith("model-"))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -m pytest tests/test_brain_registry.py -v 2>&1 | head -20`

Expected: FAIL — `ModuleNotFoundError: No module named 'agent.brain.registry'`

- [ ] **Step 3: Implement ModelRegistry**

```python
# agent/brain/registry.py
"""ModelRegistry — runtime-configurable role → model mapping.

Singleton that manages which LLM model + provider each agent role uses.
Supports runtime hot-swap (Admin API) and per-task overrides (Coordinator routing).
"""

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from agent.brain.context import get_task_model_override
from agent.brain.defaults import MODEL_CONFIGS, PROVIDERS

log = logging.getLogger("chatdada.llm")


@dataclass(frozen=True)
class ModelSpec:
    """Immutable snapshot of a role's complete model configuration."""

    role: str
    model: str
    provider: str
    client_type: str
    api_key_env: str
    endpoint_url: str | None
    provider_config: dict[str, Any]
    overrides: dict[str, Any]


class ModelRegistry:
    """Process-level registry managing role → model mappings.

    Resolution priority (highest first):
        1. task_context (explicit Coordinator routing hint)
        2. ContextVar override (per-task, via set_task_model_override)
        3. Registry current config (Admin hot-updated value)
        4. defaults.py initial values (final fallback — used by reset())
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._configs: dict[str, dict[str, Any]] = {}
        self.load_defaults()

    def load_defaults(self) -> None:
        """Load initial configs from defaults.py."""
        with self._lock:
            self._configs = {role: dict(config) for role, config in MODEL_CONFIGS.items()}

    def get(self, role: str, task_context: dict[str, Any] | None = None) -> ModelSpec:
        """Resolve the model config for a role, applying the priority chain.

        Args:
            role: Agent role name (must exist in defaults or have been registered)
            task_context: Optional per-invocation overrides from Coordinator

        Raises:
            KeyError: if role is unknown or resolved provider is unknown
        """
        with self._lock:
            base_config = self._configs.get(role)
            if base_config is None:
                raise KeyError(f"Unknown role '{role}'. Registered roles: {list(self._configs.keys())}")
            config = dict(base_config)

        # Apply ContextVar override (level 2) — role-keyed dict
        ctx_overrides = get_task_model_override()
        if ctx_overrides:
            role_override = ctx_overrides.get(role, {})
            if role_override:
                config = {**config, **{k: v for k, v in role_override.items() if v is not None}}

        # Apply task_context override (level 1 — highest)
        if task_context:
            config = {**config, **{k: v for k, v in task_context.items() if v is not None}}

        model = config.pop("model")
        provider_name = config.pop("provider")

        if provider_name not in PROVIDERS:
            raise KeyError(f"Unknown provider '{provider_name}'. Available: {list(PROVIDERS.keys())}")

        provider = PROVIDERS[provider_name]
        endpoint_url = os.environ.get(
            provider.get("endpoint_url_env", ""),
            provider.get("endpoint_url", ""),
        ) or None

        return ModelSpec(
            role=role,
            model=model,
            provider=provider_name,
            client_type=provider["client"],
            api_key_env=provider["api_key_env"],
            endpoint_url=endpoint_url,
            provider_config=provider,
            overrides=config,  # remaining keys after popping model/provider
        )

    def update(self, role: str, *, model: str | None = None, provider: str | None = None, **overrides: Any) -> None:
        """Update a single role's config at runtime. Thread-safe."""
        if provider is not None and provider not in PROVIDERS:
            raise KeyError(f"Unknown provider '{provider}'. Available: {list(PROVIDERS.keys())}")

        with self._lock:
            if role not in self._configs:
                raise KeyError(f"Unknown role '{role}'. Registered roles: {list(self._configs.keys())}")
            config = dict(self._configs[role])
            if model is not None:
                config["model"] = model
            if provider is not None:
                config["provider"] = provider
            config.update(overrides)
            self._configs[role] = config

        log.info("ModelRegistry updated role=%s model=%s provider=%s", role, config.get("model"), config.get("provider"))

    def bulk_update(self, updates: dict[str, dict[str, Any]]) -> None:
        """Atomically update multiple roles. All-or-nothing on validation."""
        # Validate first — no mutations
        for role, changes in updates.items():
            if role not in self._configs:
                raise KeyError(f"Unknown role '{role}'. Registered roles: {list(self._configs.keys())}")
            new_provider = changes.get("provider")
            if new_provider is not None and new_provider not in PROVIDERS:
                raise KeyError(f"Unknown provider '{new_provider}'. Available: {list(PROVIDERS.keys())}")

        # Apply all changes under lock
        with self._lock:
            for role, changes in updates.items():
                config = dict(self._configs[role])
                config.update(changes)
                self._configs[role] = config

        log.info("ModelRegistry bulk_update: %d roles updated", len(updates))

    def snapshot(self) -> dict[str, ModelSpec]:
        """Return immutable snapshot of all current configs."""
        with self._lock:
            roles = list(self._configs.keys())
        return {role: self.get(role) for role in roles}

    def reset(self) -> None:
        """Restore to defaults.py initial config."""
        self.load_defaults()
        log.info("ModelRegistry reset to defaults")


# Module-level singleton
registry = ModelRegistry()
```

- [ ] **Step 4: Run the tests**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -m pytest tests/test_brain_registry.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add agent/brain/registry.py tests/test_brain_registry.py
git commit -m "feat(brain): add ModelRegistry with runtime hot-swap and priority chain"
```

---

### Task 9: Wire Up `agent/brain/__init__.py` — Public API

**Files:**
- Modify: `agent/brain/__init__.py`

- [ ] **Step 1: Write the public API module**

```python
# agent/brain/__init__.py
"""Brain LLM Layer — modular, runtime-switchable LLM configuration.

Public API:
    get_llm(role, **kwargs) — logging-wrapped model
    build_chat_model(role, **kwargs) — raw BaseChatModel
    get_browser_use_llm(role, **kwargs) — browser_use adapter
    response_text(response) — extract text from LLM response
    set_thinking_level(level) — set thinking level for current async context
    set_task_model_override(overrides) — set per-task model override
    registry — ModelRegistry singleton
"""

from agent.brain.context import clear_task_model_override, get_task_model_override, set_task_model_override
from agent.brain.defaults import (
    DEFAULT_LLM_MAX_RETRIES,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS,
    MODEL_CONFIGS,
    PROVIDERS,
)
from agent.brain.factory import build_chat_model, get_browser_use_llm, get_llm, response_text
from agent.brain.providers import BrowserUseResponsesAdapter, GeminiOpenAIAdapter, MiniMaxOpenAIAdapter
from agent.brain.registry import ModelRegistry, ModelSpec, registry
from agent.brain.thinking import get_thinking_level, normalize_google_proxy_thinking_level, set_thinking_level

__all__ = [
    # Factory functions
    "get_llm",
    "build_chat_model",
    "get_browser_use_llm",
    "response_text",
    # Registry
    "registry",
    "ModelRegistry",
    "ModelSpec",
    # Context management
    "set_thinking_level",
    "get_thinking_level",
    "set_task_model_override",
    "get_task_model_override",
    "clear_task_model_override",
    # Config data (for backward compat)
    "PROVIDERS",
    "MODEL_CONFIGS",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "DEFAULT_LLM_MAX_RETRIES",
    "GOOGLE_PROXY_SUPPORTED_THINKING_LEVELS",
    # Adapter classes (for backward compat with tests)
    "MiniMaxOpenAIAdapter",
    "GeminiOpenAIAdapter",
    "BrowserUseResponsesAdapter",
]
```

- [ ] **Step 2: Verify full public API imports**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -c "from agent.brain import get_llm, build_chat_model, registry, set_thinking_level, response_text, ModelSpec; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agent/brain/__init__.py
git commit -m "feat(brain): wire up public API in __init__.py"
```

---

### Task 10: Convert `core/models.py` to Re-export Shim

**Files:**
- Modify: `core/models.py`

This is the critical backward-compatibility step. Every existing `from core.models import X` must continue to work.

- [ ] **Step 1: Replace core/models.py with re-export shim**

Back up the original file, then replace with:

```python
# core/models.py
"""
Backward-compatibility re-export shim.

All LLM configuration has moved to agent.brain.
This module re-exports the public API so existing imports continue to work.
New code should import from agent.brain directly.
"""

# Re-export everything from agent.brain
from agent.brain import *  # noqa: F401, F403

# Re-export internal symbols that tests patch directly
from agent.brain.factory import _build_client, _normalize_provider_endpoint  # noqa: F401
from agent.brain.providers.minimax import _apply_minimax_defaults  # noqa: F401
from agent.brain.thinking import (  # noqa: F401
    normalize_google_proxy_thinking_level as _normalize_google_proxy_thinking_level,
)
```

**Critical**: The test file `tests/test_models.py` patches `core.models._build_client`, `core.models.AsyncOpenAI`, `core.models.OpenAI`, and `core.models.MODEL_CONFIGS`. For `AsyncOpenAI` and `OpenAI`, these are imported at module level in the original `core/models.py`. We need to ensure they are accessible for patching:

Add to the shim:

```python
# These are patched by tests — re-export for backward compat
from openai import AsyncOpenAI, OpenAI  # noqa: F401
```

- [ ] **Step 2: Run the existing test suite to verify backward compat**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -m pytest tests/test_models.py -v 2>&1 | tail -30`

Expected: All existing tests PASS

- [ ] **Step 3: Run coordinator tests**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -m pytest tests/test_coordinator_phase2_e2e.py tests/test_coordinator_phase2_checkpoints.py -v 2>&1 | tail -20`

Expected: All tests PASS (they patch `core.models.get_llm`)

- [ ] **Step 4: Run the full test suite**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -m pytest tests/ -v 2>&1 | tail -30`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add core/models.py
git commit -m "refactor: convert core/models.py to thin re-export shim for agent.brain"
```

---

### Task 11: Add `model_hints` to Coordinator

**Files:**
- Modify: `agent/coordinator/state.py:102-141` — add `model_hints` field
- Modify: `agent/coordinator/agent.py:21-115` — output model_hints from understand_goal_node
- Modify: `agent/coordinator/agent.py:165-236` — propagate model_hints via ContextVar in execute_single_skill_node
- Modify: `agent/coordinator/prompts.py:27-134` — add Model Selection section to system prompt

- [ ] **Step 1: Add model_hints to CoordinatorState**

In `agent/coordinator/state.py`, add a new field to `CoordinatorState` at line ~118 (after `skill_input`):

```python
    # model hints (optional, from Coordinator routing)
    model_hints: dict[str, dict[str, Any]] | None
```

The full `CoordinatorState` class after the change (showing only the modified section):

```python
class CoordinatorState(TypedDict, total=False):
    # ... (existing fields unchanged)

    # single_skill 模式
    selected_skill: str | None
    skill_input: dict[str, Any] | None
    model_hints: dict[str, dict[str, Any]] | None  # NEW

    # ... (rest unchanged)
```

- [ ] **Step 2: Update understand_goal_node to parse model_hints**

In `agent/coordinator/agent.py`, in the `understand_goal_node` function, after the JSON parsing block (around line 66-85), add model_hints extraction:

Change the result dict construction (lines 74-85) to include:

```python
        result: dict[str, Any] = {
            "trace_id": trace_id,
            "execution_mode": execution_mode,
            "goal_understanding": str(parsed.get("goal_understanding", goal)),
            "skill_summary": skill_summary,
            "available_skills": skill_registry.list_skills(),
            "config": state.get("config") or CoordinatorConfig(),
            "artifact_refs": [],
            "review": {},
            "budget": {},
            "strategy_trace": [],
            "model_hints": parsed.get("model_hints"),  # NEW
        }
```

Also add `"model_hints": None` to the fallback return dict (lines 104-115).

- [ ] **Step 3: Propagate model_hints via ContextVar in execute_single_skill_node**

In `agent/coordinator/agent.py`, in `execute_single_skill_node` (line 165), before the call to `run_skill_via_adapter` (line 216), add:

```python
    # Propagate model_hints to downstream get_llm() calls via ContextVar
    model_hints = state.get("model_hints")
    if model_hints:
        from agent.brain.context import set_task_model_override
        set_task_model_override(model_hints)
```

Insert this block at line ~215, right before `result = await run_skill_via_adapter(runner, skill_input, context)`.

- [ ] **Step 4: Add Model Selection section to Coordinator system prompt**

In `agent/coordinator/prompts.py`, in `build_understand_goal_prompt()`, append the following section to the `system_prompt` string, before the closing `"""` at line 134:

```python

## 模型选择提示（可选）

根据任务特征，你可以建议下游角色使用不同的模型。仅在默认模型明显不适合时才输出 model_hints：
- 文档密集型任务 → doc_analyst 偏好 Gemini
- 简单查询任务 → 偏好更快/更便宜的模型
- 复杂推理任务 → 偏好最强模型

如果没有偏好，省略 model_hints 字段（使用默认配置）。

model_hints 格式示例：
```json
{
  "model_hints": {
    "doc_analyst": {"model": "gemini-2.5-pro"},
    "search": {"provider": "openai"}
  }
}
```"""
```

Also update the output format JSON block (line 118-127) to include `model_hints`:

```python
```json
{
  "execution_mode": "direct|single_skill|dag",
  "reasoning": "判断理由",
  "goal_understanding": "对用户目标的精炼理解",
  "selected_skill": "技能名称（仅 single_skill 模式）",
  "skill_input": {"query": "传入技能的参数（仅 single_skill 模式）"},
  "dag_strategy": "dag 执行策略（仅 dag 模式，可选）",
  "model_hints": {"role_name": {"model": "model_name"}}
}
```
```

- [ ] **Step 5: Verify coordinator still builds and runs**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -c "from agent.coordinator.agent import build_coordinator_graph; print('OK')"`

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add agent/coordinator/state.py agent/coordinator/agent.py agent/coordinator/prompts.py
git commit -m "feat(coordinator): add model_hints for per-task LLM routing with ContextVar propagation"
```

---

### Task 12: Add Admin Model Management API

**Files:**
- Modify: `web/routers/system.py`

- [ ] **Step 1: Write failing test for admin endpoints**

```python
# tests/test_admin_models_api.py
"""Tests for admin model management API endpoints."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from web.routers.system import router


def _make_test_app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return app


class TestAdminModelsAPI(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _make_test_app()
        self.client = TestClient(self.app)

    def test_get_models_returns_snapshot(self) -> None:
        resp = self.client.get("/api/admin/models")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("orchestrator", data)
        self.assertEqual(data["orchestrator"]["model"], "gpt-5.4")

    def test_put_model_updates_role(self) -> None:
        resp = self.client.put(
            "/api/admin/models/orchestrator",
            json={"model": "gpt-6"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["model"], "gpt-6")

        # Verify via GET
        resp2 = self.client.get("/api/admin/models")
        self.assertEqual(resp2.json()["orchestrator"]["model"], "gpt-6")

    def test_put_unknown_role_returns_404(self) -> None:
        resp = self.client.put(
            "/api/admin/models/nonexistent",
            json={"model": "gpt-6"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_post_reset_restores_defaults(self) -> None:
        self.client.put("/api/admin/models/orchestrator", json={"model": "gpt-6"})
        resp = self.client.post("/api/admin/models/reset")
        self.assertEqual(resp.status_code, 200)

        resp2 = self.client.get("/api/admin/models")
        self.assertEqual(resp2.json()["orchestrator"]["model"], "gpt-5.4")

    def tearDown(self) -> None:
        # Reset registry to avoid leaking state between tests
        from agent.brain.registry import registry
        registry.reset()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -m pytest tests/test_admin_models_api.py -v 2>&1 | head -20`

Expected: FAIL — endpoints don't exist yet

- [ ] **Step 3: Add admin endpoints to system router**

Add the following to `web/routers/system.py`:

```python
from agent.brain.registry import registry

class ModelUpdateRequest(BaseModel):
    model: str | None = None
    provider: str | None = None


@router.get("/api/admin/models")
async def get_all_models():
    """Return current model configuration for all roles."""
    snap = registry.snapshot()
    return {
        role: {
            "model": spec.model,
            "provider": spec.provider,
            "client_type": spec.client_type,
        }
        for role, spec in snap.items()
    }


@router.put("/api/admin/models/{role}")
async def update_model(role: str, req: ModelUpdateRequest):
    """Update model configuration for a specific role."""
    try:
        kwargs = {}
        if req.model is not None:
            kwargs["model"] = req.model
        if req.provider is not None:
            kwargs["provider"] = req.provider
        registry.update(role, **kwargs)
        spec = registry.get(role)
        return {
            "role": role,
            "model": spec.model,
            "provider": spec.provider,
            "client_type": spec.client_type,
        }
    except KeyError as exc:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": str(exc)})


@router.post("/api/admin/models/reset")
async def reset_models():
    """Reset all model configurations to defaults."""
    registry.reset()
    return {"status": "ok", "message": "All models reset to defaults"}
```

- [ ] **Step 4: Run the tests**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -m pytest tests/test_admin_models_api.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add web/routers/system.py tests/test_admin_models_api.py
git commit -m "feat(admin): add model management API endpoints for runtime hot-swap"
```

---

### Task 13: Full Integration Verification

**Files:**
- No new files — verification only

- [ ] **Step 1: Run the complete test suite**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada && python -m pytest tests/ -v 2>&1 | tail -40`

Expected: All tests PASS — existing tests via `core.models` re-export, new tests for registry and admin API

- [ ] **Step 2: Verify import paths work from both locations**

Run:
```bash
cd /Users/luozhongxu/Workspaces/chat-dada && python -c "
# Old import path (backward compat)
from core.models import get_llm, build_chat_model, response_text, set_thinking_level
from core.models import MODEL_CONFIGS, PROVIDERS, DEFAULT_LLM_TIMEOUT_SECONDS
from core.models import MiniMaxOpenAIAdapter, GeminiOpenAIAdapter

# New import path
from agent.brain import get_llm, build_chat_model, response_text, set_thinking_level
from agent.brain import registry, ModelSpec, ModelRegistry
from agent.brain import set_task_model_override, get_task_model_override

# Verify they're the same functions
import core.models as cm
import agent.brain as ab
assert cm.get_llm is ab.get_llm
assert cm.response_text is ab.response_text
print('All import paths verified OK')
"
```

Expected: `All import paths verified OK`

- [ ] **Step 3: Verify registry hot-swap works end-to-end**

Run:
```bash
cd /Users/luozhongxu/Workspaces/chat-dada && python -c "
from agent.brain import registry

# Check default
spec = registry.get('orchestrator')
assert spec.model == 'gpt-5.4', f'Expected gpt-5.4, got {spec.model}'

# Hot-swap
registry.update('orchestrator', model='gpt-6')
spec = registry.get('orchestrator')
assert spec.model == 'gpt-6', f'Expected gpt-6, got {spec.model}'

# Task context override
spec = registry.get('orchestrator', task_context={'model': 'claude-4'})
assert spec.model == 'claude-4', f'Expected claude-4, got {spec.model}'

# Reset
registry.reset()
spec = registry.get('orchestrator')
assert spec.model == 'gpt-5.4', f'Expected gpt-5.4 after reset, got {spec.model}'

print('Registry hot-swap verified OK')
"
```

Expected: `Registry hot-swap verified OK`

- [ ] **Step 4: Commit (if any fixes were needed)**

If all verifications pass without code changes, skip this step.

```bash
git add -A
git commit -m "fix: address integration issues from brain LLM redesign"
```
