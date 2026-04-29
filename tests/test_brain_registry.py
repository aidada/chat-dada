"""Tests for agent.brain.registry.ModelRegistry."""

from __future__ import annotations

import threading
import unittest

from agent.brain.defaults import ACTIVE_MODEL_CONFIG_PRESET, MODEL_CONFIGS, MODEL_CONFIG_PRESETS, PROVIDERS
from agent.brain.registry import ModelRegistry, ModelSpec


class TestModelRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ModelRegistry()

    def test_get_returns_default_config(self) -> None:
        expected = MODEL_CONFIGS["orchestrator"]
        provider_config = PROVIDERS[expected["provider"]]

        spec = self.registry.get("orchestrator")
        self.assertIsInstance(spec, ModelSpec)
        self.assertEqual(spec.role, "orchestrator")
        self.assertEqual(spec.model, expected["model"])
        self.assertEqual(spec.provider, expected["provider"])
        self.assertEqual(spec.client_type, provider_config["client"])
        self.assertEqual(spec.api_key_env, provider_config["api_key_env"])

    def test_get_unknown_role_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.registry.get("nonexistent_role")

    def test_update_changes_model(self) -> None:
        self.registry.update("orchestrator", model="gpt-6")
        spec = self.registry.get("orchestrator")
        self.assertEqual(spec.model, "gpt-6")
        self.assertEqual(spec.provider, MODEL_CONFIGS["orchestrator"]["provider"])

    def test_update_changes_provider(self) -> None:
        self.registry.update("orchestrator", provider="openai")
        spec = self.registry.get("orchestrator")
        self.assertEqual(spec.provider, "openai")
        self.assertEqual(spec.client_type, "openai")
        self.assertEqual(spec.api_key_env, "OPENAI_API_KEY")

    def test_deepseek_provider_uses_deepseek_openai_adapter_client(self) -> None:
        self.assertIn("deepseek", PROVIDERS)

        self.registry.update("orchestrator", model="deepseek-v4-pro", provider="deepseek")
        spec = self.registry.get("orchestrator")

        self.assertEqual(spec.model, "deepseek-v4-pro")
        self.assertEqual(spec.provider, "deepseek")
        self.assertEqual(spec.client_type, "deepseek_openai")
        self.assertEqual(spec.api_key_env, "DEEPSEEK_API_KEY")
        self.assertEqual(spec.endpoint_url, "https://api.deepseek.com")

    def test_deepseek_preset_is_declared(self) -> None:
        self.assertIn("deepseek", MODEL_CONFIG_PRESETS)
        self.assertEqual(
            MODEL_CONFIG_PRESETS["deepseek"]["orchestrator"],
            {"model": "deepseek-v4-pro", "provider": "deepseek"},
        )

    def test_browser_agent_follows_active_provider_family_unless_google_research(self) -> None:
        expected_by_preset = {
            "balanced": {"model": "gpt-5.5", "provider": "proxy"},
            "all_proxy": {"model": "gpt-5.5", "provider": "proxy"},
            "openai_direct": {"model": "gpt-5.5", "provider": "openai"},
            "deepseek": {"model": "deepseek-v4-pro", "provider": "deepseek"},
            "google_research": {"model": "gemini-3.1-pro-preview-customtools", "provider": "google_proxy"},
            "minimax_research": {"model": "MiniMax-M2.7-highspeed", "provider": "minimax"},
        }
        for preset_name, expected in expected_by_preset.items():
            with self.subTest(preset=preset_name):
                self.assertEqual(MODEL_CONFIG_PRESETS[preset_name]["browser_agent"], expected)

    def test_update_unknown_provider_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.registry.update("orchestrator", provider="nonexistent")

    def test_bulk_update(self) -> None:
        self.registry.bulk_update(
            {
                "orchestrator": {"model": "gpt-6"},
                "search": {"model": "gpt-6"},
            }
        )
        self.assertEqual(self.registry.get("orchestrator").model, "gpt-6")
        self.assertEqual(self.registry.get("search").model, "gpt-6")

    def test_bulk_update_atomic_on_failure(self) -> None:
        with self.assertRaises(KeyError):
            self.registry.bulk_update(
                {
                    "orchestrator": {"model": "gpt-6"},
                    "search": {"provider": "nonexistent"},
                }
            )
        self.assertEqual(self.registry.get("orchestrator").model, MODEL_CONFIGS["orchestrator"]["model"])

    def test_reset_restores_defaults(self) -> None:
        self.registry.update("orchestrator", model="gpt-6")
        self.registry.reset()
        self.assertEqual(self.registry.get("orchestrator").model, MODEL_CONFIGS["orchestrator"]["model"])

    def test_snapshot_returns_all_roles(self) -> None:
        snap = self.registry.snapshot()
        self.assertIn("orchestrator", snap)
        self.assertIn("search", snap)
        self.assertEqual(len(snap), len(MODEL_CONFIGS))

    def test_active_preset_is_declared(self) -> None:
        self.assertIn(ACTIVE_MODEL_CONFIG_PRESET, MODEL_CONFIG_PRESETS)
        self.assertEqual(MODEL_CONFIGS, MODEL_CONFIG_PRESETS[ACTIVE_MODEL_CONFIG_PRESET])

    def test_task_context_overrides_registry(self) -> None:
        spec = self.registry.get("orchestrator", task_context={"model": "gpt-6"})
        self.assertEqual(spec.model, "gpt-6")
        self.assertEqual(spec.provider, MODEL_CONFIGS["orchestrator"]["provider"])

    def test_task_context_overrides_provider(self) -> None:
        spec = self.registry.get("orchestrator", task_context={"provider": "openai"})
        self.assertEqual(spec.provider, "openai")
        self.assertEqual(spec.api_key_env, "OPENAI_API_KEY")

    def test_contextvar_override_role_keyed(self) -> None:
        from agent.brain.context import clear_task_model_override, set_task_model_override

        token = set_task_model_override(
            {
                "orchestrator": {"model": "gpt-6"},
                "search": {"provider": "openai"},
            }
        )
        try:
            spec = self.registry.get("orchestrator")
            self.assertEqual(spec.model, "gpt-6")
            spec2 = self.registry.get("search")
            self.assertEqual(spec2.provider, "openai")
            spec3 = self.registry.get("writer")
            self.assertEqual(spec3.model, MODEL_CONFIGS["writer"]["model"])
        finally:
            clear_task_model_override(token)

    def test_contextvar_token_restores_previous_override(self) -> None:
        from agent.brain.context import (
            clear_task_model_override,
            get_task_model_override,
            set_task_model_override,
        )

        outer_token = set_task_model_override({"orchestrator": {"model": "outer-model"}})
        inner_token = set_task_model_override({"orchestrator": {"model": "inner-model"}})
        try:
            self.assertEqual(
                get_task_model_override(),
                {"orchestrator": {"model": "inner-model"}},
            )
        finally:
            clear_task_model_override(inner_token)

        try:
            self.assertEqual(
                get_task_model_override(),
                {"orchestrator": {"model": "outer-model"}},
            )
        finally:
            clear_task_model_override(outer_token)

        self.assertIsNone(get_task_model_override())

    def test_priority_chain_task_context_beats_contextvar_registry_and_defaults(self) -> None:
        from agent.brain.context import clear_task_model_override, set_task_model_override

        self.registry.update("orchestrator", model="registry-model", provider="openai")
        token = set_task_model_override(
            {"orchestrator": {"model": "context-model", "provider": "anthropic"}}
        )
        try:
            spec = self.registry.get(
                "orchestrator",
                task_context={"model": "task-model", "provider": "proxy"},
            )
        finally:
            clear_task_model_override(token)

        self.assertEqual(spec.model, "task-model")
        self.assertEqual(spec.provider, "proxy")
        self.assertEqual(spec.client_type, "openai")
        self.assertEqual(spec.api_key_env, "CO_API_KEY")

    def test_concurrent_updates(self) -> None:
        errors: list[Exception] = []

        def update_loop(model: str) -> None:
            try:
                for _ in range(100):
                    self.registry.update("orchestrator", model=model)
            except Exception as exc:  # pragma: no cover - only for failures
                errors.append(exc)

        threads = [threading.Thread(target=update_loop, args=(f"model-{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(errors), 0)
        spec = self.registry.get("orchestrator")
        self.assertTrue(spec.model.startswith("model-"))
