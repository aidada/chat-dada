"""browser_use-compatible adapter over LangChain models."""

from __future__ import annotations

from typing import Any


def response_text(response: Any) -> str:
    """Extract plain text without importing the main factory module."""
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


class BrowserUseResponsesAdapter:
    """browser_use-compatible adapter over our configured LangChain models."""

    def __init__(self, role: str, model: str, llm: Any, provider: str) -> None:
        self._role = role
        self.model = model
        self._llm = llm
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def name(self) -> str:
        return str(self.model)

    @property
    def model_name(self) -> str:
        return self.model

    async def ainvoke(self, messages: list[Any], output_format: type | None = None, **kwargs: Any):
        from browser_use.llm.views import ChatInvokeCompletion

        langchain_messages = [_browser_use_message_to_langchain(message) for message in messages]
        invoke_kwargs = {k: v for k, v in kwargs.items() if k not in {"session_id", "request_type"}}

        if output_format is None:
            response = await self._llm.ainvoke(langchain_messages, **invoke_kwargs)
            return ChatInvokeCompletion(
                completion=response_text(response),
                usage=None,
                stop_reason=None,
            )

        raw_responses_llm = _unwrap_responses_chat_model(self._llm)
        if raw_responses_llm is not None and hasattr(output_format, "model_json_schema"):
            parsed = await _invoke_structured_via_responses_api(
                raw_responses_llm,
                langchain_messages,
                output_format,
                invoke_kwargs,
            )
        else:
            structured_llm = self._llm.with_structured_output(output_format)
            parsed = await structured_llm.ainvoke(langchain_messages, **invoke_kwargs)
        return ChatInvokeCompletion(
            completion=parsed,
            usage=None,
            stop_reason=None,
        )


def _browser_use_message_to_langchain(message: Any):
    from browser_use.llm.messages import AssistantMessage as BrowserAssistantMessage
    from browser_use.llm.messages import SystemMessage as BrowserSystemMessage
    from browser_use.llm.messages import UserMessage as BrowserUserMessage
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    content = _browser_use_content_to_langchain(getattr(message, "content", ""))

    if isinstance(message, BrowserSystemMessage):
        return SystemMessage(content=content)
    if isinstance(message, BrowserAssistantMessage):
        return AIMessage(content=content)
    if isinstance(message, BrowserUserMessage):
        return HumanMessage(content=content)

    role = str(getattr(message, "role", "user"))
    if role == "system":
        return SystemMessage(content=content)
    if role == "assistant":
        return AIMessage(content=content)
    return HumanMessage(content=content)


def _unwrap_responses_chat_model(llm: Any, max_depth: int = 5) -> Any | None:
    current = llm
    seen: set[int] = set()

    for _ in range(max_depth):
        if current is None:
            return None

        obj_id = id(current)
        if obj_id in seen:
            return None
        seen.add(obj_id)

        if (
            getattr(current, "use_responses_api", False)
            and hasattr(current, "root_async_client")
            and hasattr(current, "_get_request_payload")
        ):
            return current

        current = getattr(current, "_llm", None)

    return None


async def _invoke_structured_via_responses_api(
    llm: Any,
    messages: list[Any],
    output_format: type,
    invoke_kwargs: dict[str, Any],
) -> Any:
    payload = llm._get_request_payload(messages, response_format=output_format, **invoke_kwargs)
    payload.pop("stream", None)
    raw_response = await llm.root_async_client.responses.with_raw_response.parse(**payload)
    parsed_response = raw_response.parse()

    parsed = getattr(parsed_response, "output_parsed", None)
    if parsed is not None:
        return parsed

    for item in getattr(parsed_response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []) or []:
            if getattr(content, "type", None) != "output_text":
                continue
            if getattr(content, "parsed", None) is not None:
                return content.parsed
            text = getattr(content, "text", None)
            if text:
                return output_format.model_validate_json(text)

    raise ValueError(f"Responses API did not return parsed structured output for {output_format.__name__}")


def _browser_use_content_to_langchain(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []

    for part in content:
        part_type = getattr(part, "type", None)
        if part_type == "text":
            text = str(getattr(part, "text", "") or "")
            if text:
                blocks.append({"type": "text", "text": text})
                text_parts.append(text)
        elif part_type == "image_url":
            image_url = getattr(part, "image_url", None)
            url = getattr(image_url, "url", "")
            detail = getattr(image_url, "detail", "auto")
            if url:
                blocks.append({"type": "image_url", "image_url": {"url": url, "detail": detail}})
        elif part_type == "refusal":
            refusal = str(getattr(part, "refusal", "") or "")
            if refusal:
                blocks.append({"type": "text", "text": refusal})
                text_parts.append(refusal)

    if not blocks:
        return ""
    if all(block["type"] == "text" for block in blocks):
        return "\n".join(text_parts)
    return blocks


def browser_use_provider_name(provider_name: str) -> str:
    from agent.brain.defaults import PROVIDERS

    provider = PROVIDERS.get(provider_name, {})
    client_type = str(provider.get("client", "openai"))
    if client_type in {"google", "gemini_openai_adapter"}:
        return "google"
    if client_type == "anthropic":
        return "anthropic"
    return "openai"


__all__ = [
    "BrowserUseResponsesAdapter",
    "browser_use_provider_name",
]
