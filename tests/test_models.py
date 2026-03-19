from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from browser_use.llm.messages import SystemMessage as BrowserSystemMessage
from pydantic import BaseModel

from core.models import (
    DEFAULT_LLM_MAX_RETRIES,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    GeminiOpenAIAdapter,
    get_browser_use_llm,
    get_llm,
)


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


class ModelDefaultsTests(unittest.TestCase):
    def test_get_llm_applies_default_timeout_and_retries(self) -> None:
        with patch.dict(os.environ, {"CO_API_KEY": "test-key"}, clear=False):
            with patch("core.models._build_client", return_value=object()) as build_client:
                with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                    client, role, model = get_llm("search")

        self.assertEqual(role, "search")
        self.assertEqual(model, "gpt-5.4")
        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["timeout"], DEFAULT_LLM_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["max_retries"], DEFAULT_LLM_MAX_RETRIES)

    def test_get_llm_builds_google_proxy_via_adapter_without_default_medium_thinking(self) -> None:
        with patch.dict(
            os.environ,
            {"CO_API_KEY": "test-key"},
            clear=False,
        ):
            with patch.dict(
                "core.models.MODEL_CONFIGS",
                {"search": {"model": "gemini-3.1-pro-preview", "provider": "google_proxy"}},
                clear=False,
            ):
                with patch("core.models._build_client", return_value=object()) as build_client:
                    with patch("core.logger._LoggingLLM", side_effect=lambda client, role, model: (client, role, model)):
                        client, role, model = get_llm("search")

        self.assertEqual(role, "search")
        self.assertEqual(model, "gemini-3.1-pro-preview")
        self.assertIs(client, build_client.return_value)
        self.assertEqual(build_client.call_args.args, ("gemini_openai_adapter", "gemini-3.1-pro-preview", "test-key"))
        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://co.yes.vg/gemini")
        self.assertEqual(kwargs["thinking_level"], "low")

    def test_get_llm_preserves_supported_google_proxy_thinking_level(self) -> None:
        with patch.dict(
            os.environ,
            {"CO_API_KEY": "test-key"},
            clear=False,
        ):
            with patch.dict(
                "core.models.MODEL_CONFIGS",
                {"search": {"model": "gemini-3.1-pro-preview", "provider": "google_proxy"}},
                clear=False,
            ):
                with patch("core.models._build_client", return_value=object()) as build_client:
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
            with patch.dict(
                "core.models.MODEL_CONFIGS",
                {"search": {"model": "gemini-3.1-pro-preview", "provider": "google_proxy"}},
                clear=False,
            ):
                with patch("core.models._build_client", return_value=object()) as build_client:
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
        with patch("core.models.get_llm", return_value=fake_llm) as mocked_get_llm:
            adapter = get_browser_use_llm("search")
            result = self._run_async(adapter.ainvoke([BrowserSystemMessage(content="hello")], session_id="abc"))

        mocked_get_llm.assert_called_once_with("search")
        self.assertEqual(adapter.provider, "openai")
        self.assertEqual(adapter.model, "gpt-5.4")
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
        with patch.dict(
            "core.models.MODEL_CONFIGS",
            {"search": {"model": "gemini-3.1-pro-preview", "provider": "google_proxy"}},
            clear=False,
        ):
            with patch("core.models.get_llm", return_value=fake_llm) as mocked_get_llm:
                adapter = get_browser_use_llm("search")
                result = self._run_async(adapter.ainvoke([BrowserSystemMessage(content="hello")], session_id="abc"))

        mocked_get_llm.assert_called_once_with("search")
        self.assertEqual(adapter.provider, "google")
        self.assertEqual(adapter.model, "gemini-3.1-pro-preview")
        self.assertEqual(result.completion, "gemini text")
        self.assertEqual(fake_llm.kwargs, {})

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
        with patch.dict(
            "core.models.MODEL_CONFIGS",
            {"search": {"model": "gemini-3.1-pro-preview", "provider": "google_proxy"}},
            clear=False,
        ):
            with patch("core.models.get_llm", return_value=fake_llm):
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
