"""Shared BaseChatModel adapter helpers for provider wrappers."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


class ProviderChatModelAdapter(BaseChatModel):
    """Minimal BaseChatModel wrapper for custom provider adapters."""

    model: str

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return self.__class__.__name__.lower()

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model}

    @property
    def name(self) -> str:
        return str(self.model)

    @property
    def model_name(self) -> str:
        return self.model

    @abstractmethod
    def _invoke_message(
        self,
        messages: list[BaseMessage],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        """Run a synchronous chat completion and return the final message."""

    @abstractmethod
    async def _ainvoke_message(
        self,
        messages: list[BaseMessage],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        """Run an async chat completion and return the final message."""

    @staticmethod
    def _chat_result(message: BaseMessage) -> ChatResult:
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=message,
                    text=_message_text(message),
                )
            ]
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del run_manager
        message = self._invoke_message(messages, stop=stop, **kwargs)
        return self._chat_result(message)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del run_manager
        message = await self._ainvoke_message(messages, stop=stop, **kwargs)
        return self._chat_result(message)

