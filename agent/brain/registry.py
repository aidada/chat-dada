"""ModelRegistry - runtime-configurable role to model mapping."""

from __future__ import annotations

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
    """Process-level registry managing role-to-model mappings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._configs: dict[str, dict[str, Any]] = {}
        self.load_defaults()

    def load_defaults(self) -> None:
        """Load initial configs from defaults.py."""
        with self._lock:
            self._configs.clear()
            self._configs.update({role: dict(config) for role, config in MODEL_CONFIGS.items()})

    def get(self, role: str, task_context: dict[str, Any] | None = None) -> ModelSpec:
        """Resolve the model config for a role."""
        with self._lock:
            base_config = self._configs.get(role)
            if base_config is None:
                raise KeyError(f"Unknown role '{role}'. Registered roles: {list(self._configs.keys())}")
            config = dict(base_config)

        ctx_overrides = get_task_model_override()
        if ctx_overrides:
            role_override = ctx_overrides.get(role, {})
            if role_override:
                config = {**config, **{k: v for k, v in role_override.items() if v is not None}}

        if task_context:
            config = {**config, **{k: v for k, v in task_context.items() if v is not None}}

        model = str(config.pop("model"))
        provider_name = str(config.pop("provider"))
        if provider_name not in PROVIDERS:
            raise KeyError(f"Unknown provider '{provider_name}'. Available: {list(PROVIDERS.keys())}")

        provider = PROVIDERS[provider_name]
        endpoint_env = provider.get("endpoint_url_env")
        endpoint_url = (
            os.environ.get(str(endpoint_env), "") if endpoint_env else ""
        ) or provider.get("endpoint_url") or None

        return ModelSpec(
            role=role,
            model=model,
            provider=provider_name,
            client_type=str(provider["client"]),
            api_key_env=str(provider["api_key_env"]),
            endpoint_url=str(endpoint_url) if endpoint_url is not None else None,
            provider_config=dict(provider),
            overrides=config,
        )

    def update(self, role: str, *, model: str | None = None, provider: str | None = None, **overrides: Any) -> None:
        """Update a single role's config at runtime."""
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

        log.info(
            "ModelRegistry updated role=%s model=%s provider=%s",
            role,
            config.get("model"),
            config.get("provider"),
        )

    def bulk_update(self, updates: dict[str, dict[str, Any]]) -> None:
        """Atomically update multiple roles."""
        with self._lock:
            current_roles = list(self._configs.keys())

        for role, changes in updates.items():
            if role not in current_roles:
                raise KeyError(f"Unknown role '{role}'. Registered roles: {current_roles}")
            new_provider = changes.get("provider")
            if new_provider is not None and new_provider not in PROVIDERS:
                raise KeyError(f"Unknown provider '{new_provider}'. Available: {list(PROVIDERS.keys())}")

        with self._lock:
            for role, changes in updates.items():
                config = dict(self._configs[role])
                config.update(changes)
                self._configs[role] = config

        log.info("ModelRegistry bulk_update: %d roles updated", len(updates))

    def snapshot(self) -> dict[str, ModelSpec]:
        """Return an immutable snapshot of all current configs."""
        with self._lock:
            roles = list(self._configs.keys())
        return {role: self.get(role) for role in roles}

    def reset(self) -> None:
        """Restore defaults.py initial config."""
        self.load_defaults()
        log.info("ModelRegistry reset to defaults")


registry = ModelRegistry()
