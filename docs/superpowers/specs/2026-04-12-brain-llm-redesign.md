# Brain LLM Layer Redesign

> Redesign `core/models.py` into `agent/brain/` — a modular, runtime-switchable LLM layer that supports global hot-swap and per-task Coordinator routing.

## Context

`core/models.py` is a 1316-line monolith handling 6 responsibilities: provider definitions, role-to-model mapping, 3 custom adapters (MiniMax, Gemini, BrowserUse), factory functions, thinking-level ContextVar management, and serialization/logging helpers. After the harness/hands/session refactoring, LLM belongs in the Brain layer and needs runtime flexibility.

## Goals

| Goal | Description |
|------|-------------|
| **Global hot-swap** | Admin/Ops can change model configs at runtime (e.g. gpt-5.4 to gpt-6) without restart |
| **Per-task routing** | Coordinator auto-routes to optimal model based on task characteristics |
| **Brain identity** | LLM layer moves to `agent/brain/`, reflecting "LLM + Harness = Brain" |
| **File decomposition** | Split 1316-line monolith into focused modules (~100-200 lines each) |
| **Zero-disruption migration** | Existing imports continue to work throughout migration |

## Non-Goals

- Per-user model preferences (future work)
- Automatic provider failover/retry (out of scope)
- Frontend user model selection UI
- A/B testing infrastructure

## Architecture

### Directory Structure

```
agent/brain/
├── __init__.py              # Public API re-exports
├── registry.py              # ModelRegistry singleton
├── defaults.py              # MODEL_CONFIGS + PROVIDERS default data
├── factory.py               # build_client() factory, dispatches to providers/
├── thinking.py              # thinking_level ContextVar + normalization
├── context.py               # task model override ContextVar
└── providers/
    ├── __init__.py
    ├── openai.py            # ChatOpenAI thin wrapper
    ├── minimax.py           # MiniMaxOpenAIAdapter (~200 lines)
    ├── gemini.py            # GeminiOpenAIAdapter (~180 lines)
    ├── anthropic.py         # ChatAnthropic wrapper
    └── browser_use.py       # BrowserUseResponsesAdapter (~120 lines)
```

### ModelSpec

```python
@dataclass(frozen=True)
class ModelSpec:
    """Immutable snapshot of a role's complete model configuration."""
    role: str
    model: str             # e.g. "gpt-5.4"
    provider: str          # e.g. "proxy"
    client_type: str       # e.g. "openai" — resolved from PROVIDERS[provider]["client"]
    api_key_env: str       # e.g. "CO_API_KEY"
    endpoint_url: str | None
    overrides: dict        # Extra parameters (temperature, max_tokens, ...)
```

### ModelRegistry

Singleton managing runtime role-to-model mappings. Thread-safe via `threading.Lock`.

```python
class ModelRegistry:
    _instance: ClassVar[ModelRegistry | None] = None

    def get(self, role: str, task_context: dict | None = None) -> ModelSpec:
        """Resolve model config with priority chain:
        1. task_context (Coordinator routing result)
        2. _task_model_override ContextVar (per-task override)
        3. Registry current config (Admin hot-updated value)
        4. defaults.py initial values (final fallback)
        """

    def update(self, role: str, *, model: str = ..., provider: str = ..., **overrides) -> None:
        """Update a single role's config at runtime. Thread-safe. Logs the change."""

    def bulk_update(self, updates: dict[str, dict]) -> None:
        """Atomically update multiple roles. All-or-nothing."""

    def snapshot(self) -> dict[str, ModelSpec]:
        """Return immutable snapshot of all current configs. For Admin API / monitoring."""

    def reset(self) -> None:
        """Restore to defaults.py initial config. For testing and emergency rollback."""
```

### Resolution Priority Chain

```
┌─────────────────────────────────────┐
│  1. task_context (Coordinator hint) │  Highest: model hint from goal analysis
├─────────────────────────────────────┤
│  2. ContextVar override             │  Per-task, via set_task_model_override()
├─────────────────────────────────────┤
│  3. Registry current config         │  Admin hot-updated runtime value
├─────────────────────────────────────┤
│  4. defaults.py default values      │  Code-level initial values, final fallback
└─────────────────────────────────────┘
```

### Public API (agent/brain/__init__.py)

```python
# These functions maintain the same signatures as core/models.py
def get_llm(role: str, *, task_context: dict | None = None, **kwargs) -> BaseChatModel: ...
def build_chat_model(role: str, *, task_context: dict | None = None, **kwargs) -> BaseChatModel: ...
def get_browser_use_llm(role: str, **kwargs): ...
def response_text(response: Any) -> str: ...
def set_thinking_level(level: str) -> None: ...

# New: task-level override
def set_task_model_override(overrides: dict) -> None: ...

# New: registry access
registry: ModelRegistry
```

## Coordinator Smart Routing

### Where It Happens

In `coordinator/agent.py` `understand_goal_node`, which already analyzes user intent and decides execution mode (DIRECT / DAG / SINGLE_SKILL). Extend its output with `model_hints`:

```python
@dataclass
class GoalUnderstanding:
    mode: Literal["DIRECT", "DAG", "SINGLE_SKILL"]
    skill: str | None
    plan: list[Step] | None
    model_hints: dict[str, dict] | None  # NEW
    # e.g. {"doc_analyst": {"model": "gemini-2.5-pro"},
    #        "search": {"provider": "openai"}}
```

### Routing Logic

No separate "model routing model" — Coordinator's existing system prompt gets a section appended:

```
## Model Selection
Based on the task analysis, you may suggest model preferences for downstream roles.
Output model_hints only when the default is clearly suboptimal:
- Document-heavy tasks → prefer Gemini for doc_analyst
- Simple lookup tasks → prefer a faster/cheaper model
- Complex reasoning → prefer the strongest available model

If no preference, omit model_hints (defaults will be used).
```

### Propagation

model_hints flow through LangGraph state to skill execution nodes:

```
understand_goal_node
    → state["model_hints"] = {"doc_analyst": {"model": "gemini-2.5-pro"}}

execute_skill_node
    → task_context = state.get("model_hints", {}).get(current_role, {})
    → llm = get_llm(role, task_context=task_context)
```

## Admin API

New endpoints in `web/` (admin-authenticated):

| Method | Path | Action |
|--------|------|--------|
| `GET` | `/api/admin/models` | Return registry.snapshot() — all current configs |
| `PUT` | `/api/admin/models/{role}` | registry.update(role, ...) — update single role |
| `POST` | `/api/admin/models/reset` | registry.reset() — restore defaults |

Protected by existing admin authentication middleware.

## Migration Strategy

Three-step zero-disruption migration:

### Step 1: Create `agent/brain/` package

- Move all logic from `core/models.py` into the new package structure
- `core/models.py` becomes a thin re-export layer: `from agent.brain import *`
- All existing `from core.models import get_llm` imports continue to work
- All 25+ test files that patch `core.models.get_llm` continue to work

### Step 2: Introduce ModelRegistry

- `get_llm()` / `build_chat_model()` internally switch from reading module-level `MODEL_CONFIGS` dict to `registry.get()`
- External call-site API remains identical
- Add Admin API endpoints

### Step 3: Migrate import paths (gradual)

- Change call-sites from `from core.models import get_llm` to `from agent.brain import get_llm`
- This is a pure import-path change, can be done incrementally
- `core/models.py` re-export layer can remain indefinitely

## Testing

- **ModelRegistry unit tests**: update / bulk_update / reset / concurrent safety / priority chain resolution
- **Backward compatibility**: verify existing test patches on `core.models.get_llm` still work through re-export
- **Coordinator routing**: given different task types, verify model_hints output
- **Admin API integration tests**: PUT/GET/reset endpoints with auth
- **Provider adapters**: existing adapter tests move with the code, no behavior change

## Files Changed

| File | Change |
|------|--------|
| `agent/brain/__init__.py` | New — public API |
| `agent/brain/registry.py` | New — ModelRegistry |
| `agent/brain/defaults.py` | New — default configs (data from core/models.py) |
| `agent/brain/factory.py` | New — build_client() factory |
| `agent/brain/thinking.py` | New — thinking_level management |
| `agent/brain/context.py` | New — task model override ContextVar |
| `agent/brain/providers/*.py` | New — adapter code moved from core/models.py |
| `core/models.py` | Modified — becomes thin re-export layer |
| `agent/coordinator/agent.py` | Modified — add model_hints to GoalUnderstanding |
| `web/routers/admin.py` (or new) | New/Modified — Admin model management endpoints |
