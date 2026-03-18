"""
General Chat Agent — direct Q&A conversation, no tools needed.
Registered in registry as "general_chat".
"""

from collections.abc import Awaitable, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from models import get_llm, response_text
from logger import log_async


CHAT_SYSTEM = """你是一个专业、友好的AI助手，你的名字叫达达(无论是谁问你 都要回答这个名字)。
- 直接回答用户问题，简洁准确
- 必要时提供结构化的要点
- 承认不确定的地方
- 使用中文回答"""


def _normalize_input(input_data) -> tuple[str, str]:
    if isinstance(input_data, str):
        return input_data, ""
    if isinstance(input_data, dict):
        return (
            input_data.get("query", input_data.get("chat_input", str(input_data))),
            input_data.get("memory_context", ""),
        )
    return str(input_data), ""


def _build_messages(query: str, memory_context: str):
    return [
        SystemMessage(content=CHAT_SYSTEM),
        *([SystemMessage(content=memory_context)] if memory_context else []),
        HumanMessage(content=query),
    ]


def _extract_stream_delta(chunk_text: str, accumulated_text: str) -> str:
    if not chunk_text:
        return ""
    if accumulated_text and chunk_text.startswith(accumulated_text):
        return chunk_text[len(accumulated_text) :]
    return chunk_text


def _should_flush_stream(buffered_text: str) -> bool:
    if not buffered_text:
        return False
    return (
        len(buffered_text) >= 80
        or "\n" in buffered_text
        or buffered_text.endswith(("。", "！", "？", ".", "!", "?", "：", ":"))
    )


async def generate_reply(
    query: str,
    *,
    memory_context: str = "",
    on_chunk: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    llm = get_llm("orchestrator")
    messages = _build_messages(query, memory_context)

    if on_chunk is None:
        response = await llm.ainvoke(messages)
        return response_text(response)

    streamed_text = ""
    buffered_parts: list[str] = []

    try:
        async for chunk in llm.astream(messages):
            delta = _extract_stream_delta(response_text(chunk), streamed_text)
            if not delta:
                continue
            streamed_text += delta
            buffered_parts.append(delta)

            buffered_text = "".join(buffered_parts)
            if _should_flush_stream(buffered_text):
                await on_chunk(buffered_text)
                buffered_parts.clear()
    except (AttributeError, NotImplementedError):
        response = await llm.ainvoke(messages)
        return response_text(response)

    if buffered_parts:
        await on_chunk("".join(buffered_parts))

    if streamed_text.strip():
        return streamed_text

    response = await llm.ainvoke(messages)
    return response_text(response)


@log_async("agent", "general_chat")
async def run(
    input_data,
    on_chunk: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    query, memory_context = _normalize_input(input_data)
    result = await generate_reply(query, memory_context=memory_context, on_chunk=on_chunk)
    return {"status": "ok", "result": result}
