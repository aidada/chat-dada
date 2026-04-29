from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from typing import Any

from browser_use.llm.messages import SystemMessage as BrowserSystemMessage
from deepagents._models import resolve_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from pydantic import BaseModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from core.models import (
    DEFAULT_LLM_MAX_RETRIES,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DeepSeekOpenAIAdapter,
    GeminiOpenAIAdapter,
    MODEL_CONFIGS,
    MiniMaxOpenAIAdapter,
    _build_client,
    build_chat_model,
    get_browser_use_llm,
    get_llm,
)
from core.logger import _find_usage_payload
from agent.brain.registry import registry


class _DummyResponse:
    def __init__(self, body: str) -> None:
        self.headers = {"content-type": "application/json"}
        self.body = body


class GeminiOpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_translates_openai_kwargs_to_gemini_kwargs(self) -> None:
        captured: dict[str, object] = {}

        class _FakeGeminiLLM:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)
                self.client = SimpleNamespace()

        with patch("langchain_google_genai.ChatGoogleGenerativeAI", new=_FakeGeminiLLM):
            GeminiOpenAIAdapter(
                "gemini-3.1-pro-preview",
                "test-key",
                base_url="https://co.yes.vg/gemini/v1beta",
                reasoning_effort="low",
                max_tokens=2048,
                use_responses_api=True,
                output_version="responses/v1",
                temperature=0.3,
            )

        self.assertEqual(captured["model"], "gemini-3.1-pro-preview")
        self.assertEqual(captured["google_api_key"], "test-key")
        self.assertEqual(captured["base_url"], "https://co.yes.vg/gemini/v1beta")
        self.assertEqual(captured["thinking_level"], "low")
        self.assertEqual(captured["max_output_tokens"], 2048)
        self.assertEqual(captured["temperature"], 0.3)
        self.assertNotIn("reasoning_effort", captured)
        self.assertNotIn("max_tokens", captured)
        self.assertNotIn("use_responses_api", captured)
        self.assertNotIn("output_version", captured)

    async def test_adapter_logs_request_body_and_response_body_on_error(self) -> None:
        class _FakeGeminiLLM:
            def __init__(self, **kwargs) -> None:
                self.model = kwargs["model"]
                self.client = SimpleNamespace(
                    aio=SimpleNamespace(
                        models=SimpleNamespace(
                            _api_client=SimpleNamespace(async_request=self._async_request)
                        )
                    )
                )

            async def _async_request(self, http_method, path, request_dict, http_options=None):
                self.last_request = (http_method, path, request_dict, http_options)
                return _DummyResponse("not-json-body")

            async def ainvoke(self, *args, **kwargs):
                await self.client.aio.models._api_client.async_request(
                    "post",
                    f"models/{self.model}:generateContent",
                    {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
                    None,
                )
                raise RuntimeError("404 Not Found")

        with patch("langchain_google_genai.ChatGoogleGenerativeAI", new=_FakeGeminiLLM):
            llm = GeminiOpenAIAdapter(
                "gemini-3.1-pro-preview",
                "test-key",
                base_url="https://co.yes.vg/gemini/v1beta",
            )

            with self.assertLogs("chatdada.llm", level="DEBUG") as logs:
                with self.assertRaisesRegex(RuntimeError, "404 Not Found"):
                    await llm.ainvoke([])

        joined = "\n".join(logs.output)
        self.assertIn("Gemini proxy request for gemini-3.1-pro-preview", joined)
        self.assertIn('"contents": [{"role": "user", "parts": [{"text": "hello"}]}]', joined)
        self.assertIn("Gemini proxy request failed for gemini-3.1-pro-preview", joined)
        self.assertIn("not-json-body", joined)

    async def test_adapter_logs_response_body_on_success(self) -> None:
        success_body = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "thoughtSignature": "sig",
                                    "text": "An HTTP request works by sending a request and receiving a response.",
                                }
                            ],
                        }
                    }
                ],
                "usageMetadata": {"totalTokenCount": 793, "candidatesTokenCount": 45, "thoughtsTokenCount": 737},
            }
        )

        class _FakeGeminiLLM:
            def __init__(self, **kwargs) -> None:
                self.model = kwargs["model"]
                self.client = SimpleNamespace(
                    aio=SimpleNamespace(
                        models=SimpleNamespace(
                            _api_client=SimpleNamespace(async_request=self._async_request)
                        )
                    )
                )

            async def _async_request(self, http_method, path, request_dict, http_options=None):
                return _DummyResponse(success_body)

            async def ainvoke(self, *args, **kwargs):
                await self.client.aio.models._api_client.async_request(
                    "post",
                    f"models/{self.model}:generateContent",
                    {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
                    None,
                )
                return SimpleNamespace(content="ok", usage_metadata={"total_tokens": 793})

        with patch("langchain_google_genai.ChatGoogleGenerativeAI", new=_FakeGeminiLLM):
            llm = GeminiOpenAIAdapter(
                "gemini-3.1-pro-preview",
                "test-key",
                base_url="https://co.yes.vg/gemini/v1beta",
            )

            with self.assertLogs("chatdada.llm", level="DEBUG") as logs:
                result = await llm.ainvoke([])

        self.assertEqual(result.content, "ok")
        joined = "\n".join(logs.output)
        self.assertIn("Gemini proxy response for gemini-3.1-pro-preview", joined)
        self.assertIn('"thoughtSignature": "sig"', joined)
        self.assertIn('"text": "An HTTP request works by sending a request and receiving a response."', joined)

    async def test_adapter_is_base_chat_model_compatible_with_resolve_model(self) -> None:
        class _FakeGeminiLLM:
            def __init__(self, **kwargs) -> None:
                self.client = SimpleNamespace()

            def bind_tools(self, *args, **kwargs):
                return self

        with patch("langchain_google_genai.ChatGoogleGenerativeAI", new=_FakeGeminiLLM):
            adapter = GeminiOpenAIAdapter(
                "gemini-3.1-pro-preview",
                "test-key",
                base_url="https://co.yes.vg/gemini/v1beta",
            )
            bound = adapter.bind_tools([])

        self.assertIsInstance(adapter, BaseChatModel)
        self.assertIs(resolve_model(adapter), adapter)
        self.assertIsInstance(bound, BaseChatModel)


class MiniMaxOpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _patch_minimax_clients(self, parsed_payload: dict[str, Any]):
        holders: dict[str, Any] = {}

        class _FakeResponse:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            def model_dump(self, **kwargs):
                return self._payload

        class _FakeAsyncCompletions:
            def __init__(self) -> None:
                self.last_payload = None

            async def create(self, **payload):
                self.last_payload = payload
                return _FakeResponse(parsed_payload)

        class _FakeSyncCompletions:
            def __init__(self) -> None:
                self.last_payload = None

            def create(self, **payload):
                self.last_payload = payload
                return _FakeResponse(parsed_payload)

        class _FakeAsyncOpenAI:
            def __init__(self, **kwargs) -> None:
                holders["async_ctor_kwargs"] = kwargs
                self.chat = SimpleNamespace(completions=_FakeAsyncCompletions())
                holders["async_client"] = self

        class _FakeOpenAI:
            def __init__(self, **kwargs) -> None:
                holders["sync_ctor_kwargs"] = kwargs
                self.chat = SimpleNamespace(completions=_FakeSyncCompletions())
                holders["sync_client"] = self

        return (
            holders,
            patch("agent.brain.providers.minimax.AsyncOpenAI", new=_FakeAsyncOpenAI),
            patch("agent.brain.providers.minimax.OpenAI", new=_FakeOpenAI),
        )

    async def test_adapter_restores_usage_and_reasoning_details(self) -> None:
        parsed_payload = {
            "id": "cmpl-123",
            "model": "MiniMax-M2.7",
            "usage": {
                "prompt_tokens": 101,
                "completion_tokens": 29,
                "total_tokens": 130,
                "completion_tokens_details": {"reasoning_tokens": 17},
            },
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "final answer",
                        "reasoning_details": [
                            {
                                "type": "reasoning.text",
                                "id": "reasoning-1",
                                "text": "Need to call the weather tool first.",
                            }
                        ],
                    },
                }
            ],
        }

        holders, async_patch, sync_patch = self._patch_minimax_clients(parsed_payload)
        with async_patch, sync_patch:
            adapter = MiniMaxOpenAIAdapter(
                "MiniMax-M2.7",
                "test-key",
                base_url="https://api.minimaxi.com/v1",
                timeout=12,
                max_retries=3,
                extra_body={"reasoning_split": True},
            )
            previous_assistant = AIMessage(
                content="tool call pending",
                additional_kwargs={
                    "reasoning_details": [
                        {
                            "type": "reasoning.text",
                            "id": "reasoning-prev",
                            "text": "I should search before answering.",
                        }
                    ]
                },
            )
            result = await adapter.ainvoke([previous_assistant, HumanMessage(content="continue")])

        self.assertEqual(holders["async_ctor_kwargs"]["base_url"], "https://api.minimaxi.com/v1")
        self.assertEqual(holders["async_ctor_kwargs"]["timeout"], 12)
        self.assertEqual(holders["async_ctor_kwargs"]["max_retries"], 3)
        self.assertEqual(result.usage_metadata["input_tokens"], 101)
        self.assertEqual(result.usage_metadata["output_tokens"], 29)
        self.assertEqual(result.usage_metadata["total_tokens"], 130)
        self.assertEqual(result.usage_metadata["output_token_details"]["reasoning"], 17)
        self.assertEqual(result.additional_kwargs["reasoning_details"][0]["text"], "Need to call the weather tool first.")
        self.assertEqual(result.additional_kwargs["reasoning_content"], "Need to call the weather tool first.")
        self.assertEqual(result.response_metadata["usage"]["total_tokens"], 130)
        sent_messages = holders["async_client"].chat.completions.last_payload["messages"]
        self.assertEqual(sent_messages[0]["reasoning_details"][0]["text"], "I should search before answering.")

    async def test_adapter_collapses_leading_system_messages_for_minimax(self) -> None:
        parsed_payload = {
            "id": "cmpl-789",
            "model": "MiniMax-M2.7",
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
            ],
        }

        holders, async_patch, sync_patch = self._patch_minimax_clients(parsed_payload)
        with async_patch, sync_patch:
            adapter = MiniMaxOpenAIAdapter(
                "MiniMax-M2.7",
                "test-key",
                base_url="https://api.minimaxi.com/v1",
            )
            await adapter.ainvoke(
                [
                    SystemMessage(content="sys-1"),
                    SystemMessage(content="sys-2"),
                    HumanMessage(content="hello"),
                ]
            )

        sent_messages = holders["async_client"].chat.completions.last_payload["messages"]
        self.assertEqual(len(sent_messages), 2)
        self.assertEqual(sent_messages[0]["role"], "system")
        self.assertEqual(sent_messages[0]["content"], "sys-1\n\nsys-2")

    async def test_logger_finds_usage_from_minimax_raw_payload_fallback(self) -> None:
        message = AIMessage(
            content="ok",
            response_metadata={
                "_minimax_parsed_payload": {
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "total_tokens": 18,
                    }
                }
            },
        )
        payload = _find_usage_payload(message)
        self.assertEqual(payload, {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})

    async def test_adapter_bind_tools_keeps_minimax_usage_capture(self) -> None:
        parsed_payload = {
            "id": "cmpl-456",
            "model": "MiniMax-M2.7",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "search", "arguments": "{\"q\":\"x\"}"},
                            }
                        ],
                        "reasoning_details": [{"type": "reasoning.text", "text": "Use tool first."}],
                    },
                }
            ],
        }

        holders, async_patch, sync_patch = self._patch_minimax_clients(parsed_payload)

        @tool
        def search(q: str) -> str:
            """Search helper."""
            return q

        with async_patch, sync_patch:
            adapter = MiniMaxOpenAIAdapter(
                "MiniMax-M2.7",
                "test-key",
                base_url="https://api.minimaxi.com/v1",
            ).bind_tools([search])
            result = await adapter.ainvoke([HumanMessage(content="search")])

        self.assertEqual(result.usage_metadata["total_tokens"], 15)
        self.assertEqual(result.additional_kwargs["reasoning_details"][0]["text"], "Use tool first.")
        payload = holders["async_client"].chat.completions.last_payload
        self.assertIn("tools", payload)
        self.assertEqual(payload["tools"][0]["function"]["name"], "search")

    async def test_adapter_is_base_chat_model_compatible_with_resolve_model(self) -> None:
        parsed_payload = {
            "id": "cmpl-001",
            "model": "MiniMax-M2.7",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
            ],
        }

        holders, async_patch, sync_patch = self._patch_minimax_clients(parsed_payload)
        with async_patch, sync_patch:
            adapter = MiniMaxOpenAIAdapter(
                "MiniMax-M2.7",
                "test-key",
                base_url="https://api.minimaxi.com/v1",
            )
            bound = adapter.bind_tools([])

        self.assertIsInstance(adapter, BaseChatModel)
        self.assertIs(resolve_model(adapter), adapter)
        self.assertIsInstance(bound, BaseChatModel)
        self.assertIs(holders["async_client"], adapter._async_client)


class DeepSeekOpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _patch_deepseek_clients(self, parsed_payload: dict[str, Any]):
        holders: dict[str, Any] = {}

        class _FakeResponse:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            def model_dump(self, **kwargs):
                return self._payload

        class _FakeAsyncCompletions:
            def __init__(self) -> None:
                self.last_payload = None

            async def create(self, **payload):
                self.last_payload = payload
                return _FakeResponse(parsed_payload)

        class _FakeSyncCompletions:
            def __init__(self) -> None:
                self.last_payload = None

            def create(self, **payload):
                self.last_payload = payload
                return _FakeResponse(parsed_payload)

        class _FakeAsyncOpenAI:
            def __init__(self, **kwargs) -> None:
                holders["async_ctor_kwargs"] = kwargs
                self.chat = SimpleNamespace(completions=_FakeAsyncCompletions())
                holders["async_client"] = self

        class _FakeOpenAI:
            def __init__(self, **kwargs) -> None:
                holders["sync_ctor_kwargs"] = kwargs
                self.chat = SimpleNamespace(completions=_FakeSyncCompletions())
                holders["sync_client"] = self

        return (
            holders,
            patch("agent.brain.providers.deepseek.AsyncOpenAI", new=_FakeAsyncOpenAI),
            patch("agent.brain.providers.deepseek.OpenAI", new=_FakeOpenAI),
        )

    async def test_adapter_applies_thinking_defaults_and_round_trips_reasoning_content(self) -> None:
        parsed_payload = {
            "id": "chatcmpl-deepseek-1",
            "model": "deepseek-v4-pro",
            "usage": {
                "prompt_tokens": 101,
                "completion_tokens": 29,
                "total_tokens": 130,
                "completion_tokens_details": {"reasoning_tokens": 17},
            },
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "final answer",
                        "reasoning_content": "Need live prices before comparing S3 and OSS.",
                    },
                }
            ],
        }

        holders, async_patch, sync_patch = self._patch_deepseek_clients(parsed_payload)
        with async_patch, sync_patch:
            adapter = DeepSeekOpenAIAdapter(
                "deepseek-v4-pro",
                "test-key",
                base_url="https://api.deepseek.com",
                thinking_level="xhigh",
                temperature=0.2,
                top_p=0.9,
                timeout=12,
                max_retries=3,
            )
            previous_assistant = AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "I should search before answering."},
                tool_calls=[{"id": "call_1", "name": "search", "args": {"q": "aws s3 pricing"}}],
            )
            result = await adapter.ainvoke(
                [
                    previous_assistant,
                    ToolMessage(content="search result", tool_call_id="call_1"),
                    HumanMessage(content="continue"),
                ]
            )

        self.assertEqual(holders["async_ctor_kwargs"]["base_url"], "https://api.deepseek.com")
        self.assertEqual(holders["async_ctor_kwargs"]["timeout"], 12)
        self.assertEqual(holders["async_ctor_kwargs"]["max_retries"], 3)
        payload = holders["async_client"].chat.completions.last_payload
        self.assertEqual(payload["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)
        self.assertEqual(payload["messages"][0]["reasoning_content"], "I should search before answering.")
        self.assertEqual(result.content, "final answer")
        self.assertEqual(result.additional_kwargs["reasoning_content"], "Need live prices before comparing S3 and OSS.")
        self.assertEqual(result.usage_metadata["input_tokens"], 101)
        self.assertEqual(result.usage_metadata["output_token_details"]["reasoning"], 17)
        self.assertEqual(result.response_metadata["usage"]["total_tokens"], 130)

    async def test_adapter_bind_tools_keeps_reasoning_content_and_tools(self) -> None:
        parsed_payload = {
            "id": "chatcmpl-deepseek-2",
            "model": "deepseek-v4-pro",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Use search first.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "search", "arguments": "{\"q\":\"x\"}"},
                            }
                        ],
                    },
                }
            ],
        }

        holders, async_patch, sync_patch = self._patch_deepseek_clients(parsed_payload)

        @tool
        def search(q: str) -> str:
            """Search helper."""
            return q

        with async_patch, sync_patch:
            adapter = DeepSeekOpenAIAdapter(
                "deepseek-v4-pro",
                "test-key",
                base_url="https://api.deepseek.com",
            ).bind_tools([search])
            result = await adapter.ainvoke([HumanMessage(content="search")])

        payload = holders["async_client"].chat.completions.last_payload
        self.assertIn("tools", payload)
        self.assertEqual(payload["tools"][0]["function"]["name"], "search")
        self.assertEqual(result.tool_calls[0]["name"], "search")
        self.assertEqual(result.additional_kwargs["reasoning_content"], "Use search first.")

    async def test_adapter_is_base_chat_model_compatible_with_resolve_model(self) -> None:
        parsed_payload = {
            "id": "chatcmpl-deepseek-3",
            "model": "deepseek-v4-pro",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
            ],
        }

        holders, async_patch, sync_patch = self._patch_deepseek_clients(parsed_payload)
        with async_patch, sync_patch:
            adapter = DeepSeekOpenAIAdapter(
                "deepseek-v4-pro",
                "test-key",
                base_url="https://api.deepseek.com",
            )
            bound = adapter.bind_tools([])

        self.assertIsInstance(adapter, BaseChatModel)
        self.assertIs(resolve_model(adapter), adapter)
        self.assertIsInstance(bound, BaseChatModel)
        self.assertIs(holders["async_client"], adapter._async_client)


class ModelDefaultsTests(unittest.TestCase):
    def setUp(self) -> None:
        registry.reset()

    def tearDown(self) -> None:
        registry.reset()

    def test_build_client_ignores_thinking_level_for_minimax_openai(self) -> None:
        async_captured: dict[str, object] = {}
        sync_captured: dict[str, object] = {}

        class _FakeAsyncOpenAI:
            def __init__(self, **kwargs) -> None:
                async_captured.update(kwargs)
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=None))

        class _FakeOpenAI:
            def __init__(self, **kwargs) -> None:
                sync_captured.update(kwargs)
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=None))

        with patch("agent.brain.providers.minimax.AsyncOpenAI", new=_FakeAsyncOpenAI):
            with patch("agent.brain.providers.minimax.OpenAI", new=_FakeOpenAI):
                client = _build_client(
                    "minimax_openai",
                    "MiniMax-M2.7",
                    "test-key",
                    base_url="https://api.minimaxi.com/v1",
                    thinking_level="high",
                    temperature=0.3,
                )

        self.assertIsInstance(client, MiniMaxOpenAIAdapter)
        self.assertEqual(async_captured["api_key"], "test-key")
        self.assertEqual(async_captured["base_url"], "https://api.minimaxi.com/v1")
        self.assertEqual(sync_captured["api_key"], "test-key")
        self.assertEqual(client._request_defaults["temperature"], 0.3)
        self.assertNotIn("thinking_level", client._request_defaults)
        self.assertNotIn("reasoning_effort", client._request_defaults)

    def test_get_llm_applies_default_timeout_and_retries(self) -> None:
        with patch.dict(os.environ, {"CO_API_KEY": "test-key"}, clear=False):
            registry.update("search", model="gpt-5.5", provider="proxy")
            with patch("agent.brain.factory._build_client", return_value=object()) as build_client:
                with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                    client, role, model = get_llm("search")

        self.assertEqual(role, "search")
        self.assertEqual(model, "gpt-5.5")
        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["timeout"], DEFAULT_LLM_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["max_retries"], DEFAULT_LLM_MAX_RETRIES)

    def test_get_llm_builds_google_proxy_via_adapter_without_default_medium_thinking(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CO_API_KEY": "test-key",
                "YESCODE_GEMINI_BASE_URL": "https://co.yes.vg/gemini",
            },
            clear=False,
        ):
            registry.update("search", model="gemini-3.1-pro-preview", provider="google_proxy")
            with patch("agent.brain.factory._build_client", return_value=object()) as build_client:
                with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                    client, role, model = get_llm("search")

        self.assertEqual(role, "search")
        self.assertEqual(model, "gemini-3.1-pro-preview")
        self.assertIs(client, build_client.return_value)
        self.assertEqual(build_client.call_args.args, ("gemini_openai_adapter", "gemini-3.1-pro-preview", "test-key"))
        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://co.yes.vg/gemini")
        self.assertEqual(kwargs["thinking_level"], "low")

    def test_core_models_model_configs_patch_dict_remains_compatible(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CO_API_KEY": "test-key",
                "YESCODE_GEMINI_BASE_URL": "https://co.yes.vg/gemini",
            },
            clear=False,
        ):
            with patch.dict(
                "core.models.MODEL_CONFIGS",
                {"search": {"model": "gemini-3.1-pro-preview", "provider": "google_proxy"}},
                clear=False,
            ):
                with patch("agent.brain.factory._build_client", return_value=object()) as build_client:
                    with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                        client, role, model = get_llm("search")

        self.assertEqual(role, "search")
        self.assertEqual(model, "gemini-3.1-pro-preview")
        self.assertIs(client, build_client.return_value)
        self.assertEqual(build_client.call_args.args, ("gemini_openai_adapter", "gemini-3.1-pro-preview", "test-key"))

    def test_get_llm_builds_minimax_provider_via_openai_compatible_client(self) -> None:
        with patch.dict(
            os.environ,
            {"MINIMAX_API_KEY": "test-key"},
            clear=False,
        ):
            registry.update("search", model="MiniMax-M2.7", provider="minimax")
            with patch("agent.brain.factory._build_client", return_value=object()) as build_client:
                with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                    client, role, model = get_llm("search")

        self.assertEqual(role, "search")
        self.assertEqual(model, "MiniMax-M2.7")
        self.assertIs(client, build_client.return_value)
        self.assertEqual(build_client.call_args.args, ("minimax_openai", "MiniMax-M2.7", "test-key"))
        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://api.minimaxi.com/v1")
        self.assertEqual(kwargs["extra_body"], {"reasoning_split": True})
        self.assertEqual(kwargs["disable_streaming"], "tool_calling")
        self.assertNotIn("thinking_level", kwargs)

    def test_get_llm_builds_deepseek_provider_via_openai_compatible_client(self) -> None:
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "test-key"},
            clear=False,
        ):
            registry.update("search", model="deepseek-v4-pro", provider="deepseek")
            with patch("agent.brain.factory._build_client", return_value=object()) as build_client:
                with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                    client, role, model = get_llm("search")

        self.assertEqual(role, "search")
        self.assertEqual(model, "deepseek-v4-pro")
        self.assertIs(client, build_client.return_value)
        self.assertEqual(build_client.call_args.args, ("deepseek_openai", "deepseek-v4-pro", "test-key"))
        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://api.deepseek.com")
        self.assertIn("thinking_level", kwargs)

    def test_get_llm_uses_deepseek_env_base_url_override(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://api.deepseek.com/",
            },
            clear=False,
        ):
            registry.update("search", model="deepseek-v4-flash", provider="deepseek")
            with patch("agent.brain.factory._build_client", return_value=object()) as build_client:
                with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                    get_llm("search")

        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://api.deepseek.com")

    def test_get_llm_uses_minimax_env_base_url_override(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MINIMAX_API_KEY": "test-key",
                "MINIMAX_BASE_URL": "https://api.minimaxi.com/v1/",
            },
            clear=False,
        ):
            registry.update("search", model="MiniMax-M2.7-highspeed", provider="minimax")
            with patch("agent.brain.factory._build_client", return_value=object()) as build_client:
                with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                    get_llm("search", thinking_level="high")

        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://api.minimaxi.com/v1")
        self.assertEqual(kwargs["extra_body"], {"reasoning_split": True})
        self.assertEqual(kwargs["disable_streaming"], "tool_calling")
        self.assertNotIn("thinking_level", kwargs)

    def test_get_llm_merges_minimax_extra_body_and_normalizes_temperature(self) -> None:
        with patch.dict(
            os.environ,
            {"MINIMAX_API_KEY": "test-key"},
            clear=False,
        ):
            registry.update("search", model="MiniMax-M2.7", provider="minimax")
            with patch("agent.brain.factory._build_client", return_value=object()) as build_client:
                with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                    get_llm(
                        "search",
                        temperature=0,
                        extra_body={"custom_flag": True},
                    )

        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["temperature"], 1.0)
        self.assertEqual(kwargs["extra_body"], {"reasoning_split": True, "custom_flag": True})
        self.assertEqual(kwargs["disable_streaming"], "tool_calling")

    def test_build_chat_model_returns_base_chat_model_for_minimax_provider(self) -> None:
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}, clear=False):
            registry.update("search", model="MiniMax-M2.7", provider="minimax")
            client = build_chat_model("search")

        self.assertIsInstance(client, BaseChatModel)
        self.assertIs(resolve_model(client), client)

    def test_build_chat_model_returns_base_chat_model_for_google_proxy_provider(self) -> None:
        class _FakeGeminiLLM:
            def __init__(self, **kwargs) -> None:
                self.client = SimpleNamespace()

        with patch.dict(
            os.environ,
            {
                "CO_API_KEY": "test-key",
                "YESCODE_GEMINI_BASE_URL": "https://co.yes.vg/gemini/v1beta",
            },
            clear=False,
        ):
            registry.update("search", model="gemini-3.1-pro-preview", provider="google_proxy")
            with patch("langchain_google_genai.ChatGoogleGenerativeAI", new=_FakeGeminiLLM):
                client = build_chat_model("search")

        self.assertIsInstance(client, BaseChatModel)
        self.assertIs(resolve_model(client), client)

    def test_get_llm_preserves_supported_google_proxy_thinking_level(self) -> None:
        with patch.dict(
            os.environ,
            {"CO_API_KEY": "test-key"},
            clear=False,
        ):
            registry.update("search", model="gemini-3.1-pro-preview", provider="google_proxy")
            with patch("agent.brain.factory._build_client", return_value=object()) as build_client:
                with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                    get_llm("search", thinking_level="high")

        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["thinking_level"], "high")

    def test_get_llm_strips_google_proxy_v1beta_suffix_from_env_base_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CO_API_KEY": "test-key",
                "YESCODE_GEMINI_BASE_URL": "https://co.yes.vg/gemini/v1beta",
            },
            clear=False,
        ):
            registry.update("search", model="gemini-3.1-pro-preview", provider="google_proxy")
            with patch("agent.brain.factory._build_client", return_value=object()) as build_client:
                with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                    get_llm("search", thinking_level="high")

        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://co.yes.vg/gemini")

    def test_get_browser_use_llm_uses_openai_compatible_base_url(self) -> None:
        class _FakeLLM:
            async def ainvoke(self, messages, **kwargs):
                self.messages = messages
                self.kwargs = kwargs
                return "adapter text"

            def with_structured_output(self, schema):
                raise AssertionError("structured output should not be used in this test")

        fake_llm = _FakeLLM()
        registry.update("search", model="gpt-5.5", provider="proxy")
        with patch("agent.brain.factory.get_llm", return_value=fake_llm) as mocked_get_llm:
            adapter = get_browser_use_llm("search")
            result = self._run_async(adapter.ainvoke([BrowserSystemMessage(content="hello")], session_id="abc"))

        mocked_get_llm.assert_called_once_with("search")
        self.assertEqual(adapter.provider, "openai")
        self.assertEqual(adapter.model, "gpt-5.5")
        self.assertEqual(result.completion, "adapter text")
        self.assertEqual(fake_llm.kwargs, {})

    def test_get_browser_use_llm_uses_google_provider_for_google_proxy_role(self) -> None:
        class _FakeLLM:
            async def ainvoke(self, messages, **kwargs):
                self.kwargs = kwargs
                return "gemini text"

            def with_structured_output(self, schema):
                self.schema = schema
                return self

        fake_llm = _FakeLLM()
        registry.update("search", model="gemini-3.1-pro-preview", provider="google_proxy")
        with patch("agent.brain.factory.get_llm", return_value=fake_llm) as mocked_get_llm:
            adapter = get_browser_use_llm("search")
            result = self._run_async(adapter.ainvoke([BrowserSystemMessage(content="hello")], session_id="abc"))

        mocked_get_llm.assert_called_once_with("search")
        self.assertEqual(adapter.provider, "google")
        self.assertEqual(adapter.model, "gemini-3.1-pro-preview")
        self.assertEqual(result.completion, "gemini text")
        self.assertEqual(fake_llm.kwargs, {})

    def test_get_browser_use_llm_browser_agent_uses_active_preset_config(self) -> None:
        class _FakeLLM:
            async def ainvoke(self, messages, **kwargs):
                return "browser text"

            def with_structured_output(self, schema):
                return self

        registry.reset()
        expected = dict(MODEL_CONFIGS["browser_agent"])
        with patch("agent.brain.factory.get_llm", return_value=_FakeLLM()) as mocked_get_llm:
            adapter = get_browser_use_llm("browser_agent")

        mocked_get_llm.assert_called_once_with("browser_agent")
        self.assertEqual(adapter.model, expected["model"])

    def test_get_browser_use_llm_structured_output_uses_responses_api(self) -> None:
        class _StructuredOut(BaseModel):
            value: str

        class _FakeParsedResponse:
            def __init__(self) -> None:
                self.output_parsed = _StructuredOut(value="ok")

        class _FakeRawResponse:
            def parse(self):
                return _FakeParsedResponse()

        async def fake_responses_parse(**kwargs):
            self.responses_parse_kwargs = kwargs
            return _FakeRawResponse()

        async def fail_chat_parse(**kwargs):
            raise AssertionError("chat.completions.parse should not be used")

        async def fail_chat_create(**kwargs):
            raise AssertionError("chat.completions.create should not be used")

        with patch.dict(os.environ, {"CO_API_KEY": "test-key"}, clear=False):
            registry.update("search", model="gpt-5.5", provider="proxy")
            adapter = get_browser_use_llm("search")

        raw_llm = adapter._llm._llm
        raw_llm.root_async_client.responses.with_raw_response.parse = fake_responses_parse
        raw_llm.root_async_client.chat.completions.with_raw_response.parse = fail_chat_parse
        raw_llm.async_client.with_raw_response.create = fail_chat_create

        result = self._run_async(
            adapter.ainvoke([BrowserSystemMessage(content="hello")], output_format=_StructuredOut)
        )

        self.assertEqual(result.completion.value, "ok")
        self.assertIn("input", self.responses_parse_kwargs)
        self.assertNotIn("response_format", self.responses_parse_kwargs)

    def test_get_browser_use_llm_google_structured_output_uses_langchain_fallback(self) -> None:
        class _StructuredOut(BaseModel):
            value: str

        class _FakeStructuredLLM:
            async def ainvoke(self, messages, **kwargs):
                return _StructuredOut(value="gemini-ok")

        class _FakeLLM:
            def __init__(self) -> None:
                self.called_with = None

            def with_structured_output(self, schema):
                self.called_with = schema
                return _FakeStructuredLLM()

        fake_llm = _FakeLLM()
        registry.update("search", model="gemini-3.1-pro-preview", provider="google_proxy")
        with patch("agent.brain.factory.get_llm", return_value=fake_llm):
            adapter = get_browser_use_llm("search")
            result = self._run_async(
                adapter.ainvoke([BrowserSystemMessage(content="hello")], output_format=_StructuredOut)
            )

        self.assertEqual(adapter.provider, "google")
        self.assertIs(fake_llm.called_with, _StructuredOut)
        self.assertEqual(result.completion.value, "gemini-ok")

    def _run_async(self, coro):
        import asyncio

        return asyncio.run(coro)
