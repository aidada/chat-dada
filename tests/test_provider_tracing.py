from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.load.dump import dumpd

from agent.brain.providers.deepseek import DeepSeekOpenAIAdapter
from agent.brain.providers.gemini import GeminiOpenAIAdapter
from agent.brain.providers.minimax import MiniMaxOpenAIAdapter


def test_minimax_adapter_dumpd_serializes_name_as_string() -> None:
    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs) -> None:
            del kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    class _FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            del kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    with (
        patch("agent.brain.providers.minimax.AsyncOpenAI", new=_FakeAsyncOpenAI),
        patch("agent.brain.providers.minimax.OpenAI", new=_FakeOpenAI),
    ):
        adapter = MiniMaxOpenAIAdapter(
            "MiniMax-M2.7-highspeed",
            "test-key",
            base_url="https://api.minimaxi.com/v1",
        )

    dumped = dumpd(adapter)

    assert adapter.model_dump()["name"] == "MiniMax-M2.7-highspeed"
    assert dumped["name"] == "MiniMax-M2.7-highspeed"


def test_gemini_adapter_dumpd_serializes_name_as_string() -> None:
    class _FakeGeminiLLM:
        def __init__(self, **kwargs) -> None:
            del kwargs
            self.client = SimpleNamespace()

    with patch("langchain_google_genai.ChatGoogleGenerativeAI", new=_FakeGeminiLLM):
        adapter = GeminiOpenAIAdapter(
            "gemini-3.1-pro-preview",
            "test-key",
            base_url="https://co.yes.vg/gemini/v1beta",
        )

    dumped = dumpd(adapter)

    assert adapter.model_dump()["name"] == "gemini-3.1-pro-preview"
    assert dumped["name"] == "gemini-3.1-pro-preview"


def test_deepseek_adapter_dumpd_serializes_name_as_string() -> None:
    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs) -> None:
            del kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    class _FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            del kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    with (
        patch("agent.brain.providers.deepseek.AsyncOpenAI", new=_FakeAsyncOpenAI),
        patch("agent.brain.providers.deepseek.OpenAI", new=_FakeOpenAI),
    ):
        adapter = DeepSeekOpenAIAdapter(
            "deepseek-v4-pro",
            "test-key",
            base_url="https://api.deepseek.com",
        )

    dumped = dumpd(adapter)

    assert adapter.model_dump()["name"] == "deepseek-v4-pro"
    assert dumped["name"] == "deepseek-v4-pro"
