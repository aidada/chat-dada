"""Gemini OpenAI adapter for the Google AI proxy."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import PrivateAttr

from agent.brain.providers.base import ProviderChatModelAdapter
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


class GeminiOpenAIAdapter(ProviderChatModelAdapter):
    """Adapt our OpenAI-oriented LangChain usage to a Gemini-compatible proxy endpoint."""

    use_responses_api: ClassVar[bool] = False
    _base_url: str | None = PrivateAttr(default=None)
    _llm: Any = PrivateAttr(default=None)

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        llm: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model=model)
        self._base_url = base_url

        if llm is not None:
            self._llm = llm
            return

        if not api_key:
            raise ValueError("GeminiOpenAIAdapter requires an api_key when llm is not provided")

        from langchain_google_genai import ChatGoogleGenerativeAI

        translated_kwargs = _translate_openai_kwargs_to_gemini(kwargs)
        self._llm = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            **({"base_url": base_url} if base_url else {}),
            **translated_kwargs,
        )

    @property
    def provider(self) -> str:
        return "google"

    @property
    def name(self) -> str:
        return str(self.model)

    @property
    def model_name(self) -> str:
        return self.model

    def _wrapped_llm(self) -> Any:
        private = object.__getattribute__(self, "__pydantic_private__")
        if not private or private.get("_llm") is None:
            raise AttributeError(f"{type(self).__name__!r} object has no wrapped llm")
        return private["_llm"]

    def _adapter_base_url(self) -> str | None:
        private = object.__getattribute__(self, "__pydantic_private__")
        return None if not private else private.get("_base_url")

    @staticmethod
    def _coerce_message(response: Any) -> BaseMessage:
        if isinstance(response, BaseMessage):
            return response
        content = getattr(response, "content", response)
        usage_metadata = getattr(response, "usage_metadata", None)
        if not (
            isinstance(usage_metadata, dict)
            and "input_tokens" in usage_metadata
            and "output_tokens" in usage_metadata
        ):
            usage_metadata = None
        return AIMessage(
            content=content if isinstance(content, str) else str(content),
            additional_kwargs=dict(getattr(response, "additional_kwargs", {}) or {}),
            response_metadata=dict(getattr(response, "response_metadata", {}) or {}),
            usage_metadata=usage_metadata,
            id=getattr(response, "id", None),
        )

    def _get_sync_api_client(self) -> Any | None:
        llm = self._wrapped_llm()
        return getattr(getattr(getattr(llm, "client", None), "models", None), "_api_client", None)

    def _get_async_api_client(self) -> Any | None:
        llm = self._wrapped_llm()
        return getattr(
            getattr(getattr(getattr(llm, "client", None), "aio", None), "models", None),
            "_api_client",
            None,
        )

    @staticmethod
    def _capture_response(capture: dict[str, Any], path: Any, response: Any) -> None:
        capture.update(
            {
                "path": path,
                "headers": dict(getattr(response, "headers", {}) or {}),
                "body": getattr(response, "body", None),
            }
        )

    def _wrap_sync_request(self, api_client: Any, capture: dict[str, Any]) -> tuple[Any | None, Any | None]:
        original_request = getattr(api_client, "request", None)
        if not callable(original_request):
            return None, None

        def wrapped_request(http_method, path, request_dict, http_options=None):
            _capture_google_proxy_request(capture, http_method, path, request_dict, http_options)
            _log_google_proxy_request(self.model, capture)
            response = original_request(http_method, path, request_dict, http_options)
            self._capture_response(capture, path, response)
            _log_google_proxy_response(self.model, capture)
            return response

        return original_request, wrapped_request

    def _wrap_async_request(self, api_client: Any, capture: dict[str, Any]) -> tuple[Any | None, Any | None]:
        original_async_request = getattr(api_client, "async_request", None)
        if not callable(original_async_request):
            return None, None

        async def wrapped_async_request(http_method, path, request_dict, http_options=None):
            _capture_google_proxy_request(capture, http_method, path, request_dict, http_options)
            _log_google_proxy_request(self.model, capture)
            response = await original_async_request(http_method, path, request_dict, http_options)
            self._capture_response(capture, path, response)
            _log_google_proxy_response(self.model, capture)
            return response

        return original_async_request, wrapped_async_request

    def _invoke_message(
        self,
        messages: list[Any],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        call_kwargs = _translate_openai_kwargs_to_gemini(
            {"stop": stop, **kwargs} if stop is not None else kwargs
        )
        llm = self._wrapped_llm()
        api_client = self._get_sync_api_client()
        if api_client is None:
            return self._coerce_message(llm.invoke(messages, **call_kwargs))

        capture: dict[str, Any] = {}
        original_request, wrapped_request = self._wrap_sync_request(api_client, capture)
        if original_request is None or wrapped_request is None:
            return self._coerce_message(llm.invoke(messages, **call_kwargs))

        api_client.request = wrapped_request
        try:
            return self._coerce_message(llm.invoke(messages, **call_kwargs))
        except Exception as exc:
            _log_google_proxy_failure(self.model, capture, exc)
            raise
        finally:
            api_client.request = original_request

    async def _ainvoke_message(
        self,
        messages: list[Any],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        call_kwargs = _translate_openai_kwargs_to_gemini(
            {"stop": stop, **kwargs} if stop is not None else kwargs
        )
        llm = self._wrapped_llm()
        api_client = self._get_async_api_client()
        if api_client is None:
            return self._coerce_message(await llm.ainvoke(messages, **call_kwargs))

        capture: dict[str, Any] = {}
        original_async_request, wrapped_async_request = self._wrap_async_request(api_client, capture)
        if original_async_request is None or wrapped_async_request is None:
            return self._coerce_message(await llm.ainvoke(messages, **call_kwargs))

        api_client.async_request = wrapped_async_request
        try:
            return self._coerce_message(await llm.ainvoke(messages, **call_kwargs))
        except Exception as exc:
            _log_google_proxy_failure(self.model, capture, exc)
            raise
        finally:
            api_client.async_request = original_async_request

    async def astream(self, *args: Any, **kwargs: Any):
        call_kwargs = _translate_openai_kwargs_to_gemini(kwargs)
        llm = self._wrapped_llm()
        api_client = self._get_async_api_client()
        if api_client is None:
            async for chunk in llm.astream(*args, **call_kwargs):
                yield chunk
            return

        capture: dict[str, Any] = {}
        original_async_request, wrapped_async_request = self._wrap_async_request(api_client, capture)
        if original_async_request is None or wrapped_async_request is None:
            async for chunk in llm.astream(*args, **call_kwargs):
                yield chunk
            return

        api_client.async_request = wrapped_async_request
        try:
            async for chunk in llm.astream(*args, **call_kwargs):
                yield chunk
        except Exception as exc:
            _log_google_proxy_failure(self.model, capture, exc)
            raise
        finally:
            api_client.async_request = original_async_request

    def bind_tools(self, *args: Any, **kwargs: Any) -> "GeminiOpenAIAdapter":
        llm = self._wrapped_llm()
        return GeminiOpenAIAdapter(
            self.model,
            base_url=self._adapter_base_url(),
            llm=llm.bind_tools(*args, **kwargs),
        )

    def with_structured_output(self, *args: Any, **kwargs: Any) -> "GeminiOpenAIAdapter":
        llm = self._wrapped_llm()
        return GeminiOpenAIAdapter(
            self.model,
            base_url=self._adapter_base_url(),
            llm=llm.with_structured_output(*args, **kwargs),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped_llm(), name)


__all__ = ["GeminiOpenAIAdapter"]
