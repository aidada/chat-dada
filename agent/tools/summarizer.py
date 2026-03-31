"""
Summarizer Tool — single LLM call to summarize text.
"""
from langchain_core.messages import HumanMessage, SystemMessage
from core.models import get_llm, response_text
from core.logger import log_async


@log_async("tool", "summarizer")
async def run(input_data) -> dict:
    """
    Summarize text content.

    Args:
        input_data: str (text) or dict with "text" key
    """
    if isinstance(input_data, str):
        text = input_data
    elif isinstance(input_data, dict):
        text = input_data.get("text", str(input_data))
    else:
        text = str(input_data)

    llm = get_llm("writer")
    messages = [
        SystemMessage(content="你是一个专业的摘要助手。将以下内容总结为简洁的要点。保留关键数据和结论。"),
        HumanMessage(content=text),
    ]
    response = await llm.ainvoke(messages)
    return {"status": "ok", "result": response_text(response)}
